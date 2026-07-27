"""Tests for .github/workflows/sweep-repository.yaml (the sweep's emit contract).

result.json is the only payload that crosses a job boundary: the workflow's
"Compose result.json" step writes it into an artifact, and the record job's
scripts/build_envelopes.py reads it back. For as long as the workflow lived in
another repository nothing could test the pair, and the two halves drifted
silently -- a rosdep failure left build_outcome null, the recorder read that as
inconclusive and skipped the record, and the browse site labelled a genuinely
broken package "not yet swept" for three weeks.

So these tests run the REAL step. They parse the workflow, extract the python
heredoc verbatim, execute it against fixture .sweep-results.txt /
.sweep-present.txt files, validate what it writes against
schema/sweep-result.schema.json, and push it through build_envelopes to the
verdict the site would render. Nothing is re-implemented here: a reworded echo
in the verdict step or a reshaped payload in the emitter fails these tests
rather than quietly turning every package inconclusive.

Tests for the reader's own behaviour live in tests/test_build_envelopes.py.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import build_envelopes
import jsonschema
import pytest
import yaml

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

COMPOSE_STEP = "Compose result.json"
ROSDEP_STEP = "Install dependencies (rosdep)"
EXPRESSION_RE = re.compile(r"\$\{\{(.*?)\}\}")

DISTRO = "jazzy"
REPO_NAME = "awesome_tools"
REPOSITORY = "https://github.com/example-org/awesome_tools"
SHA = "c" * 40
VERSION = "1.9.0"

# The ${{ }} expressions the workflow's env: blocks reference, resolved to
# fixture values. Substituting from the step's OWN env mapping (rather than
# hand-writing the variable names here) is what makes a renamed or deleted env
# key fail these tests instead of passing them.
EXPRESSIONS = {
    "inputs.ros_distro": DISTRO,
    "inputs.repo_name": REPO_NAME,
    "inputs.repository": REPOSITORY,
    "inputs.ref_kind": "tag",
    "inputs.ref_value": "1.2.0",
    "needs.resolve.outputs.autoware_version": VERSION,
    "steps.resolve.outputs.sha": SHA,
    "steps.scan.outputs.present": "",
    "inputs.packages": "pkg_a",
}


def workflow_step(repo_root: Path, name: str) -> dict:
    workflow = yaml.safe_load((repo_root / ".github/workflows/sweep-repository.yaml").read_text())
    steps = workflow["jobs"]["validate"]["steps"]
    step = next((s for s in steps if s.get("name") == name), None)
    assert step is not None, f"no step named {name!r} in sweep-repository.yaml"
    return step


def step_env(step: dict, expressions: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve a step's env: block against EXPRESSIONS.

    Every ${{ }} the workflow references must be known here, so adding one to
    the workflow without teaching the tests about it fails loudly rather than
    resolving to an empty string.
    """
    resolved = {**EXPRESSIONS, **(expressions or {})}
    env = {}
    for key, raw in (step.get("env") or {}).items():
        value = str(raw)
        for match in EXPRESSION_RE.finditer(value):
            expr = match.group(1).strip()
            assert expr in resolved, f"unknown expression ${{{{ {expr} }}}} in env: {key}"
            value = value.replace(match.group(0), resolved[expr])
        env[key] = value
    return env


def run_step(
    repo_root: Path,
    tmp_path: Path,
    name: str,
    env: dict[str, str],
    stubs: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Execute a workflow step's ENTIRE run: body under bash, in tmp_path.

    Running the whole body (not an extracted fragment) means any shell the step
    gains -- a second heredoc, a post-processing filter -- is executed by these
    tests too. `stubs` are executables placed first on PATH, so the steps that
    shell out to rosdep/apt-get/colcon can run offline.
    """
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    for binary, body in (stubs or {}).items():
        path = bindir / binary
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(workflow_step(repo_root, name)["run"])
    return subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env={
            "PATH": f"{bindir}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GITHUB_WORKSPACE": str(tmp_path),
            **env,
        },
        capture_output=True,
        text=True,
    )


def compose(
    repo_root: Path,
    tmp_path: Path,
    packages: str,
    results: str | None = None,
    present: str | None = None,
    expressions: dict[str, str] | None = None,
) -> dict:
    """Run the real 'Compose result.json' step; return what it wrote.

    `results` and `present` are the literal contents of the .sweep-*.txt files
    the earlier steps leave behind; None means the step that writes it never
    ran, which is exactly what an infrastructure fault looks like on disk.
    """
    if results is not None:
        (tmp_path / ".sweep-results.txt").write_text(results)
    if present is not None:
        (tmp_path / ".sweep-present.txt").write_text(present)
    env = step_env(
        workflow_step(repo_root, COMPOSE_STEP),
        {"inputs.packages": packages, **(expressions or {})},
    )
    proc = run_step(repo_root, tmp_path, COMPOSE_STEP, env)
    assert proc.returncode == 0, proc.stderr
    return json.loads((tmp_path / "result.json").read_text())


def result_schema(repo_root: Path) -> dict:
    return json.loads((repo_root / "schema/sweep-result.schema.json").read_text())


def assert_valid(repo_root: Path, result: dict) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(result_schema(repo_root)).iter_errors(result), key=str
    )
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors]


def row(packages: str) -> dict:
    return {
        "ros_distro": DISTRO,
        "repo_name": REPO_NAME,
        "repository": REPOSITORY,
        "ref_kind": "tag",
        "ref_value": "1.2.0",
        "packages": packages,
    }


def statuses(result: dict, packages: str) -> dict[str, str]:
    """Run the real reader over the real emitter's output."""
    envelopes, _ = build_envelopes.envelopes_for_row(
        row(packages), result, "eager", "2026-06-11T12:00:00Z", "https://example.com/run/42"
    )
    return {e["package_name"]: e["record"]["status"] for e in envelopes}


# ---------------------------------------------------------------------------
# the emitter still writes what the schema declares
# ---------------------------------------------------------------------------


def test_compose_emits_a_schema_valid_payload(repo_root, tmp_path):
    result = compose(
        repo_root, tmp_path, packages="pkg_a", results="pkg_a success success\n", present="pkg_a\n"
    )
    assert_valid(repo_root, result)


def test_compose_top_level_keys_match_the_schema_exactly(repo_root, tmp_path):
    """No key drifts in or out without the schema noticing.

    additionalProperties:false catches an added key and `required` catches a
    removed one, but only if a payload actually reaches a validator. Comparing
    the key sets directly means a rename is caught even for the fields no
    reader consumes (repository, ref), which no behavioural test would miss.
    """
    result = compose(repo_root, tmp_path, packages="pkg_a", results="", present="")
    schema = result_schema(repo_root)
    assert set(result) == set(schema["required"]) == set(schema["properties"])


def test_package_outcome_keys_match_the_schema_exactly(repo_root, tmp_path):
    result = compose(
        repo_root, tmp_path, packages="pkg_a", results="pkg_a success success\n", present="pkg_a\n"
    )
    declared = result_schema(repo_root)["$defs"]["package_outcome"]
    assert (
        set(result["packages"]["pkg_a"]) == set(declared["required"]) == set(declared["properties"])
    )


# ---------------------------------------------------------------------------
# the outcome vocabulary is closed on both sides
# ---------------------------------------------------------------------------


def test_schema_outcome_enum_is_exactly_what_status_for_understands(repo_root):
    """The drift that would silently blind the pipeline.

    status_for() compares against the bare literals "success" and "failure";
    anything else is inconclusive. If the verdict step's echo were reworded,
    every package would go inconclusive with no crash and no annotation, and
    every row would re-sweep forever. Pin the vocabulary to the reader.
    """
    outcome = result_schema(repo_root)["$defs"]["package_outcome"]
    for field in ("build_outcome", "test_outcome"):
        assert set(outcome["properties"][field]["enum"]) == {"success", "failure", None}

    # Every non-null enum member must be a literal status_for actually branches
    # on, in both positions.
    assert (
        build_envelopes.status_for(
            {"present": True, "build_outcome": "success", "test_outcome": "success"}
        )
        == "pass"
    )
    assert (
        build_envelopes.status_for(
            {"present": True, "build_outcome": "failure", "test_outcome": None}
        )
        == "fail"
    )
    assert (
        build_envelopes.status_for(
            {"present": True, "build_outcome": "success", "test_outcome": "failure"}
        )
        == "fail"
    )


def test_every_writer_of_sweep_results_uses_the_known_vocabulary(repo_root):
    """The emitter's other half: the shell that writes .sweep-results.txt.

    The compose heredoc copies tokens 2 and 3 of each line verbatim (mapping
    only the "skipped" sentinel to null), so the vocabulary is really set by the
    echo statements in the rosdep and verdict steps. Match every line that
    writes the file and require each one to be recognised -- an allowlist grep
    would silently stop checking if a writer were added in another syntax
    (printf, tee, a plain >), and the tokens it wrote would sail through into
    build_outcome and blow up only in production.
    """
    workflow = yaml.safe_load((repo_root / ".github/workflows/sweep-repository.yaml").read_text())
    body = "\n".join(
        s["run"] for s in workflow["jobs"]["validate"]["steps"] if isinstance(s.get("run"), str)
    )
    writers = [
        ln.strip() for ln in body.splitlines() if re.search(r">>?\s*\.sweep-results\.txt", ln)
    ]
    truncations = [ln for ln in writers if ln.startswith(": >")]
    echoes = [ln for ln in writers if ln not in truncations]
    assert (
        len(truncations) == 1
    ), f"expected exactly one truncation of .sweep-results.txt: {truncations}"

    recognised = re.compile(r'^echo "\$\{pkg\} (\w+) (\w+)" >> \.sweep-results\.txt$')
    unrecognised = [ln for ln in echoes if not recognised.match(ln)]
    assert not unrecognised, f"unrecognised .sweep-results.txt writer(s): {unrecognised}"
    assert echoes, "no .sweep-results.txt writer found; did the echo format change?"

    for build, test in (recognised.match(ln).groups() for ln in echoes):
        assert build in {"success", "failure"}, build
        assert test in {"success", "failure", "skipped"}, test


def test_compose_step_has_exactly_one_heredoc_and_the_env_the_tests_supply(repo_root):
    """Pin the shape these tests rely on, so a refactor cannot mute them.

    run_step() executes the whole `run:` body, so an added heredoc IS exercised
    -- but the fixture inputs would no longer describe it. And step_env() only
    resolves the env: block it is given, so this is where a key added to the
    workflow without a fixture value gets caught.
    """
    step = workflow_step(repo_root, COMPOSE_STEP)
    assert len(re.findall(r"python3 - <<'EOF'", step["run"])) == 1
    assert set(step["env"]) == {
        "ROS_DISTRO_INPUT",
        "AUTOWARE_VERSION",
        "REPO_NAME",
        "REPOSITORY",
        "REF_KIND",
        "REF_VALUE",
        "RESOLVED_SHA",
        "PACKAGES",
    }


# ---------------------------------------------------------------------------
# emit -> parse round trips, per verdict shape
# ---------------------------------------------------------------------------


def test_green_package_round_trips_to_pass(repo_root, tmp_path):
    result = compose(
        repo_root, tmp_path, packages="pkg_a", results="pkg_a success success\n", present="pkg_a\n"
    )
    assert_valid(repo_root, result)
    assert statuses(result, "pkg_a") == {"pkg_a": "pass"}


def test_build_failure_round_trips_to_fail(repo_root, tmp_path):
    """The vision_pilot shape: build failed, so tests never ran.

    The verdict step writes the "skipped" sentinel, the emitter erases it to
    null, and the reader must still call this conclusive -- a fail, not an
    inconclusive skip. Getting this wrong is what left a broken package
    labelled "not yet swept".
    """
    result = compose(
        repo_root, tmp_path, packages="pkg_a", results="pkg_a failure skipped\n", present="pkg_a\n"
    )
    assert_valid(repo_root, result)
    assert result["packages"]["pkg_a"]["test_outcome"] is None
    assert statuses(result, "pkg_a") == {"pkg_a": "fail"}


def test_own_test_failure_round_trips_to_fail(repo_root, tmp_path):
    result = compose(
        repo_root, tmp_path, packages="pkg_a", results="pkg_a success failure\n", present="pkg_a\n"
    )
    assert_valid(repo_root, result)
    assert statuses(result, "pkg_a") == {"pkg_a": "fail"}


def test_sibling_verdicts_stay_independent(repo_root, tmp_path):
    """One bad manifest must not blind the packages that built fine."""
    result = compose(
        repo_root,
        tmp_path,
        packages="pkg_bad pkg_ok",
        results="pkg_bad failure skipped\npkg_ok success success\n",
        present="pkg_bad\npkg_ok\n",
    )
    assert_valid(repo_root, result)
    assert statuses(result, "pkg_bad pkg_ok") == {"pkg_bad": "fail", "pkg_ok": "pass"}


# ---------------------------------------------------------------------------
# inconclusive shapes: nulls survive as nulls, and are never recorded
# ---------------------------------------------------------------------------


def test_missing_results_file_emits_nulls_not_a_verdict(repo_root, tmp_path):
    """The exact payload a rosdep/infrastructure fault produces.

    The build and verdict steps never ran, so .sweep-results.txt does not
    exist. Both outcomes must be explicit nulls -- schema-valid, and
    inconclusive to the reader. Recording either a pass or a fail here would
    fabricate a verdict for something that was never validated.
    """
    result = compose(repo_root, tmp_path, packages="pkg_a", results=None, present="pkg_a\n")
    assert_valid(repo_root, result)
    assert result["packages"]["pkg_a"] == {
        "present": True,
        "build_outcome": None,
        "test_outcome": None,
    }
    assert statuses(result, "pkg_a") == {}


def test_absent_package_is_present_false_and_inconclusive(repo_root, tmp_path):
    result = compose(repo_root, tmp_path, packages="pkg_gone", results="", present="")
    assert_valid(repo_root, result)
    assert result["packages"]["pkg_gone"]["present"] is False
    assert statuses(result, "pkg_gone") == {}


def test_empty_resolved_sha_is_schema_valid_but_skips_the_row(repo_root, tmp_path):
    """The reader owns this gate, so the schema must not pre-empt it.

    An empty resolved_sha is the documented "clone never completed" signal. It
    has to survive validation for envelopes_for_row() to reject it with the
    message that explains it.
    """
    result = compose(
        repo_root,
        tmp_path,
        packages="pkg_a",
        results="pkg_a success success\n",
        present="pkg_a\n",
        expressions={"steps.resolve.outputs.sha": ""},
    )
    assert_valid(repo_root, result)
    _, skips = build_envelopes.envelopes_for_row(
        row("pkg_a"), result, "eager", "2026-06-11T12:00:00Z", "https://example.com/run/42"
    )
    assert len(skips) == 1 and "resolved_sha" in skips[0]


def test_empty_autoware_version_is_schema_valid_but_skips_the_row(repo_root, tmp_path):
    result = compose(
        repo_root,
        tmp_path,
        packages="pkg_a",
        results="pkg_a success success\n",
        present="pkg_a\n",
        expressions={"needs.resolve.outputs.autoware_version": ""},
    )
    assert_valid(repo_root, result)
    _, skips = build_envelopes.envelopes_for_row(
        row("pkg_a"), result, "eager", "2026-06-11T12:00:00Z", "https://example.com/run/42"
    )
    assert len(skips) == 1 and "autoware_version" in skips[0]


# ---------------------------------------------------------------------------
# the schema rejects the drifts it exists to catch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda r: r["packages"]["pkg_a"].update(build_outcome="ok"), "build_outcome"),
        (lambda r: r["packages"]["pkg_a"].update(test_outcome="passed"), "test_outcome"),
        (lambda r: r["packages"]["pkg_a"].update(present="true"), "present"),
        (lambda r: r["packages"]["pkg_a"].pop("test_outcome"), "test_outcome"),
        (lambda r: r.update(schema=3), "schema"),
        (lambda r: r.pop("resolved_sha"), "resolved_sha"),
        (lambda r: r["ref"].update(kind="commit"), "kind"),
    ],
)
def test_schema_rejects_drift(repo_root, tmp_path, mutate, expected):
    result = compose(
        repo_root, tmp_path, packages="pkg_a", results="pkg_a success success\n", present="pkg_a\n"
    )
    mutate(result)
    validator = jsonschema.Draft202012Validator(result_schema(repo_root))
    row_errors, package_errors = build_envelopes.result_schema_errors(result, validator)
    errors = row_errors + [e for details in package_errors.values() for e in details]
    assert errors, f"schema accepted a drifted payload ({expected})"
    assert any(expected in e for e in errors), errors


# ---------------------------------------------------------------------------
# the rosdep step's attribution parser, against real rosdep output
# ---------------------------------------------------------------------------

# Verbatim from run 30194228074, the nightly that first exposed the gap: rosdep
# names each package whose manifest declares a key it cannot resolve, under a
# fixed header. Parsing this is what turns "the install step died" into a real
# per-package verdict, so the fixture is real output, not a paraphrase.
ROSDEP_UNRESOLVED_LOG = """\
reading in sources list data from /etc/ros/rosdep/sources.list.d
Hit https://raw.githubusercontent.com/ros/rosdistro/master/rosdep/base.yaml
updated cache in /github/home/.ros/rosdep/sources.cache
ERROR: the following packages/stacks could not have their rosdep keys resolved
to system dependencies:
vision_pilot: Cannot locate rosdep definition for [opencv]
"""

ROSDEP_INFRA_LOG = """\
Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease
E: Failed to fetch http://archive.ubuntu.com/ubuntu/pool/main/libc6.deb
E: Unable to fetch some archives, maybe run apt-get update?
"""

# A stub rosdep: `update` succeeds, `install` replays a captured log and fails,
# unless --skip-keys names every unresolvable key (the retry the fix depends on).
ROSDEP_STUB = """
[ "$1" = "update" ] && exit 0
log=$(cat "$FIXTURE_LOG")
[ -z "$log" ] && exit 0
if [ -n "$SKIP_SATISFIES" ] && [[ "$*" == *"--skip-keys"* ]]; then exit 0; fi
printf '%s\\n' "$log"
exit 1
"""

COLCON_STUB = """
# `colcon list -p --packages-up-to X Y` -> src/X src/Y ; --names-only -> X Y
names=""
seen_upto=0
for arg in "$@"; do
  if [ "$seen_upto" = 1 ]; then
    case "$arg" in --*) seen_upto=0;; *) names="$names $arg";; esac
  fi
  [ "$arg" = "--packages-up-to" ] && seen_upto=1
done
for n in $names; do
  if [[ "$*" == *"-p"* ]]; then echo "src/$n"; else echo "$n"; fi
done
"""


def run_rosdep_step(repo_root: Path, tmp_path: Path, log: str, present: str, skip_works=True):
    """Run the ENTIRE rosdep step offline: parse, attribution, retry, exit code."""
    (tmp_path / "fixture.log").write_text(log)
    (tmp_path / ".sweep-present.txt").write_text(present)
    return run_step(
        repo_root,
        tmp_path,
        ROSDEP_STEP,
        {
            "ROS_DISTRO_INPUT": DISTRO,
            "PRESENT": present.replace("\n", " ").strip(),
            "FIXTURE_LOG": str(tmp_path / "fixture.log"),
            "SKIP_SATISFIES": "1" if skip_works else "",
        },
        stubs={"rosdep": ROSDEP_STUB, "apt-get": "exit 0", "colcon": COLCON_STUB},
    )


def failed_seed(tmp_path: Path) -> list[str]:
    path = tmp_path / ".sweep-failed.txt"
    return path.read_text().split() if path.is_file() else []


def test_rosdep_step_seeds_the_declaring_package_into_sweep_failed(repo_root, tmp_path):
    """The regression this whole contract exists for.

    Parsing rosdep's output is only half of it: the parse has to reach
    .sweep-failed.txt, because that is the file the verdict step matches
    closures against. Without the seed, --skip-keys lets a package with an
    unresolvable key BUILD, and the recorder writes a green record for
    something nobody can install.
    """
    proc = run_rosdep_step(repo_root, tmp_path, ROSDEP_UNRESOLVED_LOG, "vision_pilot\n")
    assert proc.returncode == 0, proc.stderr
    assert failed_seed(tmp_path) == ["vision_pilot"]
    assert "unresolvable rosdep key(s) [opencv] declared by: vision_pilot" in proc.stdout


def test_rosdep_step_retries_with_skip_keys_so_siblings_still_install(repo_root, tmp_path):
    """A bad manifest must not stop its siblings' dependencies installing.

    The stub only succeeds when --skip-keys is passed, so a step that gave up
    after the first failure would exit non-zero here.
    """
    proc = run_rosdep_step(repo_root, tmp_path, ROSDEP_UNRESOLVED_LOG, "vision_pilot\npkg_ok\n")
    assert proc.returncode == 0, proc.stderr
    assert failed_seed(tmp_path) == ["vision_pilot"]


def test_rosdep_step_hard_fails_on_an_infrastructure_fault(repo_root, tmp_path):
    """A rosdep failure naming nobody must never be attributed to a package.

    Exit non-zero with nothing seeded: the build and verdict steps are then
    skipped, every outcome stays null, and the recorder treats the row as
    inconclusive rather than inventing a red for whatever was registered.
    """
    proc = run_rosdep_step(repo_root, tmp_path, ROSDEP_INFRA_LOG, "pkg_a\n")
    assert proc.returncode != 0
    assert failed_seed(tmp_path) == []
    assert "infrastructure fault" in proc.stdout


def test_rosdep_step_leaves_no_seed_when_everything_resolves(repo_root, tmp_path):
    proc = run_rosdep_step(repo_root, tmp_path, "", "pkg_a\n")
    assert proc.returncode == 0, proc.stderr
    assert failed_seed(tmp_path) == []


def test_rosdep_step_does_not_seed_the_header_lines(repo_root, tmp_path):
    """`ERROR:` and `to system dependencies:` both end in a colon.

    A looser "<word>: <text>" parse would seed .sweep-failed.txt with junk that
    the verdict step then matches against real closures.
    """
    run_rosdep_step(repo_root, tmp_path, ROSDEP_UNRESOLVED_LOG, "vision_pilot\n")
    assert failed_seed(tmp_path) == ["vision_pilot"]


def test_build_step_appends_to_the_seed_rather_than_truncating_it(repo_root, tmp_path):
    """The one-character dependency the rosdep fix rests on.

    The build step used to overwrite .sweep-failed.txt with the packages colcon
    named. If it still did, the rosdep seed would be erased between the two
    steps and a bad manifest that happens to compile would record as a pass.
    """
    body = workflow_step(repo_root, "Build (union of registered packages)")["run"]
    assert ">> .sweep-failed.txt" in body
    assert not re.search(r"[^>]> \.sweep-failed\.txt", body)


# ---------------------------------------------------------------------------
# what the schema deliberately tolerates
# ---------------------------------------------------------------------------


def test_unknown_keys_are_tolerated_at_runtime(repo_root, tmp_path):
    """An added field must never cost a sweep its history.

    additionalProperties:false would turn a backwards-compatible emitter change
    -- someone stamping a runner_os or image_digest -- into every row being
    skipped and the whole run's pass/fail history being thrown away. The reader
    ignores keys it does not know, so validation does too. A RENAME is still
    caught, by `required` on the name that went missing, and an added key is
    caught at PR time by the key-set tests above.
    """
    validator = jsonschema.Draft202012Validator(result_schema(repo_root))
    result = compose(
        repo_root, tmp_path, packages="pkg_a", results="pkg_a success success\n", present="pkg_a\n"
    )
    result["runner_os"] = "ubuntu-24.04"
    result["packages"]["pkg_a"]["duration_s"] = 12
    assert build_envelopes.result_schema_errors(result, validator) == ([], {})
    assert statuses(result, "pkg_a") == {"pkg_a": "pass"}


def test_one_packages_drift_does_not_drop_its_siblings(repo_root, tmp_path):
    """Per-package attribution, so a monorepo is not blinded by one bad entry.

    The verdict step writes each package's line from a different echo, so one
    can drift while the rest stay correct. The drifted package is skipped like
    any other inconclusive outcome; the sibling still records.
    """
    validator = jsonschema.Draft202012Validator(result_schema(repo_root))
    result = compose(
        repo_root,
        tmp_path,
        packages="pkg_bad pkg_ok",
        results="pkg_bad success success\npkg_ok success success\n",
        present="pkg_bad\npkg_ok\n",
    )
    result["packages"]["pkg_bad"]["build_outcome"] = "ok"

    row_errors, package_errors = build_envelopes.result_schema_errors(result, validator)
    assert row_errors == []
    assert set(package_errors) == {"pkg_bad"}

    envelopes, skips = build_envelopes.envelopes_for_row(
        row("pkg_bad pkg_ok"),
        result,
        "eager",
        "2026-06-11T12:00:00Z",
        "https://example.com/run/42",
        package_errors,
    )
    assert [e["package_name"] for e in envelopes] == ["pkg_ok"]
    assert len(skips) == 1 and "pkg_bad" in skips[0] and "build_outcome" in skips[0]
