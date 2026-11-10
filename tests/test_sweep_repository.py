"""Tests for the build/test workflows and the sweep's emit contract.

The build workflow's "Compose result.json" step writes an artifact, the test
workflow fills in test outcomes using the cached build, and the record job's
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
    "inputs.autoware_version": VERSION,
    "inputs.base_image_stage": "core-devel",
    "steps.resolve.outputs.sha": SHA,
    "steps.scan.outputs.present": "",
    "inputs.packages": "pkg_a",
    "toJSON(steps)": "{}",
    "steps.logs.outputs.artifact-url": "",
}


def workflow_step(repo_root: Path, name: str, workflow_name="build-repository") -> dict:
    workflow = yaml.safe_load((repo_root / f".github/workflows/{workflow_name}.yaml").read_text())
    steps = next(iter(workflow["jobs"].values()))["steps"]
    step = next((s for s in steps if s.get("name") == name), None)
    assert step is not None, f"no step named {name!r} in {workflow_name}.yaml"
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
    workflow_name="build-repository",
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
    script.write_text(workflow_step(repo_root, name, workflow_name)["run"])
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
    assert set(result) == set(schema["properties"])
    # base_image_stage is provenance: the reader records a verdict without it.
    assert set(schema["required"]) == set(schema["properties"]) - {"base_image_stage"}


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
    workflow = yaml.safe_load((repo_root / ".github/workflows/build-repository.yaml").read_text())
    body = "\n".join(
        s["run"] for s in workflow["jobs"]["build"]["steps"] if isinstance(s.get("run"), str)
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
        "BASE_IMAGE_STAGE",
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


def records(result: dict, packages: str) -> list[dict]:
    envelopes, _ = build_envelopes.envelopes_for_row(
        row(packages), result, "eager", "2026-06-11T12:00:00Z", "https://example.com/run/42"
    )
    return [envelope["record"] for envelope in envelopes]


@pytest.mark.parametrize("stage", ["core-devel", "core-devel-cuda"])
def test_image_stage_round_trips_into_a_schema_valid_record(repo_root, tmp_path, stage):
    result = compose(
        repo_root,
        tmp_path,
        packages="pkg_a",
        results="pkg_a success success\n",
        present="pkg_a\n",
        expressions={"inputs.base_image_stage": stage},
    )
    assert_valid(repo_root, result)
    assert result["base_image_stage"] == stage
    (record,) = records(result, "pkg_a")
    assert record["base_image_stage"] == stage
    history_schema = json.loads((repo_root / "schema/history-record.schema.json").read_text())
    jsonschema.Draft202012Validator(history_schema).validate(record)


@pytest.mark.parametrize("stage", ["", "Core Devel", None, 7])
def test_a_missing_or_malformed_image_stage_never_costs_the_verdict(repo_root, tmp_path, stage):
    result = compose(
        repo_root, tmp_path, packages="pkg_a", results="pkg_a success success\n", present="pkg_a\n"
    )
    if stage is None:
        del result["base_image_stage"]
    else:
        result["base_image_stage"] = stage
    # main() runs the schema gate first, and a row error there skips the row.
    validator = jsonschema.Draft202012Validator(result_schema(repo_root))
    assert build_envelopes.result_schema_errors(result, validator) == ([], {})
    (record,) = records(result, "pkg_a")
    assert record["status"] == "pass"
    assert "base_image_stage" not in record


def test_test_workflow_keeps_the_image_stage_of_the_build(repo_root, tmp_path):
    """test-repository.yaml rewrites result.json, and the stage must survive."""
    result = compose(
        repo_root,
        tmp_path,
        packages="pkg_a",
        results="pkg_a success success\n",
        present="pkg_a\n",
        expressions={"inputs.base_image_stage": "core-devel-cuda"},
    )
    path = tmp_path / "_result" / "result.json"
    path.parent.mkdir()
    path.write_text(json.dumps(result))
    proc = run_step(
        repo_root,
        tmp_path,
        "Select successfully built packages",
        {"GITHUB_OUTPUT": str(tmp_path / "output")},
        workflow_name="test-repository",
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(path.read_text())["base_image_stage"] == "core-devel-cuda"


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
        expressions={"inputs.autoware_version": ""},
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


# ---------------------------------------------------------------------------
# independent build/test checks and the cache/result handoff
# ---------------------------------------------------------------------------


def test_split_workflows_allow_testing_successful_siblings_after_build_failure(repo_root):
    workflow = yaml.safe_load((repo_root / ".github/workflows/sweep-repository.yaml").read_text())
    build = workflow["jobs"]["build"]
    test = workflow["jobs"]["test"]
    assert build["uses"] == "./.github/workflows/build-repository.yaml"
    assert test["uses"] == "./.github/workflows/test-repository.yaml"
    assert "build" in test["needs"]
    assert "!cancelled()" in test["if"]
    assert "needs.build.outputs.has-tests == 'true'" in test["if"]
    assert test["with"]["cache_key"] == "${{ needs.build.outputs.cache-key }}"
    for key in ("ros_distro", "autoware_version", "base_image_stage", "runs-on"):
        assert build["with"][key] == test["with"][key]


def test_resolve_job_detects_the_image_stage_unless_the_caller_sets_it(repo_root):
    workflow = yaml.safe_load((repo_root / ".github/workflows/sweep-repository.yaml").read_text())
    assert workflow[True]["workflow_call"]["inputs"]["base_image_stage"]["default"] == ""
    resolve = workflow["jobs"]["resolve"]
    stage = "${{ inputs.base_image_stage || steps.stage.outputs.base_image_stage }}"
    assert resolve["outputs"]["base_image_stage"] == stage
    steps = {step.get("id"): step for step in resolve["steps"]}
    assert steps["stage"]["uses"].startswith(
        "autowarefoundation/autoware-index-github-actions/.github/actions/detect-base-image-stage@"
    )
    assert steps["stage"]["if"] == "inputs.base_image_stage == ''"
    assert "inputs.dependency_packages" in steps["stage"]["with"]["packages"]
    assert steps["latest"]["with"]["base_image_stage"] == stage
    for job in ("build", "test"):
        assert (
            workflow["jobs"][job]["with"]["base_image_stage"]
            == "${{ needs.resolve.outputs.base_image_stage }}"
        )
    assert resolve["outputs"]["source_sha"] == "${{ steps.clone.outputs.sha }}"
    assert steps["clone"]["if"] == "inputs.base_image_stage == ''"
    assert (
        workflow["jobs"]["build"]["with"]["checkout_ref"]
        == "${{ needs.resolve.outputs.source_sha }}"
    )
    assert resolve["timeout-minutes"] > 0


RESOLVE_CLONE_STEP = "Clone repository and index dependencies"


def source_repository(git_repo, name: str):
    (git_repo.path / "package.xml").write_text(f"<package><name>{name}</name></package>")
    return git_repo.commit("first")


def run_resolve_clone(repo_root, tmp_path, repository, ref_value, dependencies="[]", stubs=None):
    env = step_env(
        workflow_step(repo_root, RESOLVE_CLONE_STEP, "sweep-repository"),
        {
            "inputs.repository": str(repository),
            "inputs.ref_value": ref_value,
            "inputs.dependency_repositories": dependencies,
        },
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "github-output"
    proc = run_step(
        repo_root,
        workspace,
        RESOLVE_CLONE_STEP,
        {**env, "GITHUB_OUTPUT": str(output)},
        stubs=stubs,
        workflow_name="sweep-repository",
    )
    return proc, workspace, output


# A git that refuses a shallow fetch, as a server that gives no commit by SHA.
NO_SHALLOW_GIT = (
    'for a in "$@"; do [ "$a" = "--depth" ] && exit 128; done\nexec /usr/bin/git "$@"\n'
)


@pytest.mark.parametrize("ref_kind", ["tag", "branch", "sha"])
def test_resolve_clone_step_checks_out_the_ref_and_reports_its_commit(
    repo_root, tmp_path, git_repo, ref_kind
):
    first = source_repository(git_repo, "pkg_a")
    git_repo.git("tag", "1.0.0")
    (git_repo.path / "later.txt").write_text("later")
    second = git_repo.commit("second")
    ref_value, expected = {
        "tag": ("1.0.0", first),
        "branch": ("main", second),
        "sha": (first, first),
    }[ref_kind]

    proc, workspace, output = run_resolve_clone(repo_root, tmp_path, git_repo.path, ref_value)

    assert proc.returncode == 0, proc.stderr
    assert output.read_text() == f"sha={expected}\n"
    assert (workspace / "src/repository/package.xml").is_file()
    assert (workspace / "src/repository/later.txt").exists() == (expected == second)


@pytest.mark.parametrize("ref_kind", ["tag", "branch", "sha"])
def test_resolve_clone_step_falls_back_to_the_full_history(repo_root, tmp_path, git_repo, ref_kind):
    first = source_repository(git_repo, "pkg_a")
    git_repo.git("tag", "1.0.0")
    (git_repo.path / "later.txt").write_text("later")
    second = git_repo.commit("second")
    ref_value, expected = {
        "tag": ("1.0.0", first),
        "branch": ("main", second),
        "sha": (first, first),
    }[ref_kind]

    proc, _, output = run_resolve_clone(
        repo_root, tmp_path, git_repo.path, ref_value, stubs={"git": NO_SHALLOW_GIT}
    )

    assert proc.returncode == 0, proc.stderr
    assert output.read_text() == f"sha={expected}\n"


@pytest.mark.parametrize("field", ["repository", "ref_value"])
def test_resolve_clone_step_refuses_a_value_that_git_reads_as_an_option(
    repo_root, tmp_path, git_repo, field
):
    source_repository(git_repo, "pkg_a")
    marker = tmp_path / "ran"
    bad = f"--upload-pack=touch {marker};"
    repository, ref_value = (bad, "main") if field == "repository" else (git_repo.path, bad)

    proc, _, _ = run_resolve_clone(repo_root, tmp_path, repository, ref_value)

    assert proc.returncode != 0
    assert "refusing a repository or ref" in proc.stderr
    assert not marker.exists()


def source_files(root: Path) -> set[str]:
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def test_resolve_clone_step_uses_the_source_layout_of_the_build(repo_root, tmp_path, git_repo):
    """The detection must read the tree that the build job builds.

    Both jobs run their real clone steps here, and the two src/ trees must
    hold the same files.
    """
    source_repository(git_repo, "pkg_a")
    dependencies = json.dumps(
        [
            {
                "repo_name": "dep_repo",
                "repository": str(git_repo.path),
                "ref_kind": "branch",
                "ref_value": "main",
            }
        ]
    )
    expressions = {
        "inputs.repository": str(git_repo.path),
        "inputs.ref_value": "main",
        "inputs.checkout_ref": "",
        "inputs.dependency_repositories": dependencies,
    }

    proc, resolve_workspace, _ = run_resolve_clone(
        repo_root, tmp_path, git_repo.path, "main", dependencies
    )
    assert proc.returncode == 0, proc.stderr

    build_workspace = tmp_path / "build-workspace"
    build_workspace.mkdir()
    for name in ("Clone repository", "Clone index dependencies"):
        env = step_env(workflow_step(repo_root, name), expressions)
        proc = run_step(
            repo_root, build_workspace, name, {**env, "GITHUB_WORKSPACE": str(build_workspace)}
        )
        assert proc.returncode == 0, proc.stderr

    expected = {"repository/package.xml", "index-dependencies/dep_repo/package.xml"}
    assert source_files(resolve_workspace / "src") == expected
    assert source_files(build_workspace / "src") == expected


def test_resolve_clone_step_fails_on_a_ref_that_does_not_exist(repo_root, tmp_path, git_repo):
    source_repository(git_repo, "pkg_a")
    proc, _, output = run_resolve_clone(repo_root, tmp_path, git_repo.path, "no-such-ref")
    assert proc.returncode != 0
    assert not output.exists() or output.read_text() == ""
    assert f"::error::cannot clone {str(git_repo.path)!r} at ref 'no-such-ref'" in proc.stderr
    assert "Traceback" not in proc.stderr


def dependency_row(repository, repo_name="dep_repo", **extra) -> str:
    row = {"repo_name": repo_name, "repository": str(repository), "ref_kind": "branch"}
    return json.dumps([{**row, "ref_value": "main", **extra}])


def run_build_dependency_clone(repo_root, tmp_path, dependencies):
    name = "Clone index dependencies"
    env = step_env(workflow_step(repo_root, name), {"inputs.dependency_repositories": dependencies})
    workspace = tmp_path / "build-workspace"
    workspace.mkdir()
    proc = run_step(repo_root, workspace, name, {**env, "GITHUB_WORKSPACE": str(workspace)})
    return proc, workspace


@pytest.mark.parametrize("repo_name", ["../../../escape", "a/b", "..", "Dep", "dep\n", ""])
def test_clone_steps_refuse_a_dependency_name_that_is_not_a_registry_key(
    repo_root, tmp_path, git_repo, repo_name
):
    source_repository(git_repo, "pkg_a")
    dependencies = dependency_row(git_repo.path, repo_name)

    resolve, resolve_workspace, _ = run_resolve_clone(
        repo_root, tmp_path, git_repo.path, "main", dependencies
    )
    build, build_workspace = run_build_dependency_clone(repo_root, tmp_path, dependencies)

    for proc, workspace in ((resolve, resolve_workspace), (build, build_workspace)):
        assert proc.returncode != 0
        assert "::error::refusing an index dependency name" in proc.stderr
        assert "Traceback" not in proc.stderr
        assert not (workspace / "src/index-dependencies").exists()
    assert not (tmp_path / "escape").exists()


def test_clone_steps_use_the_repository_key_rule_of_the_registry_schema(repo_root):
    schema = json.loads((repo_root / "schema/distribution.schema.json").read_text())
    pattern = schema["properties"]["repositories"]["propertyNames"]["pattern"]
    resolve = workflow_step(repo_root, RESOLVE_CLONE_STEP, "sweep-repository")
    build = workflow_step(repo_root, "Clone index dependencies")
    for step in (resolve, build):
        assert f're.compile(r"{pattern}")' in step["run"]


def test_build_clones_a_dependency_at_the_commit_that_the_detection_read(
    repo_root, tmp_path, git_repo
):
    """A branch tip that moves after the detection must not reach the build."""
    first = source_repository(git_repo, "pkg_a")
    proc, _, output = run_resolve_clone(
        repo_root, tmp_path, git_repo.path, "main", dependency_row(git_repo.path)
    )
    assert proc.returncode == 0, proc.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs["sha"] == first
    assert json.loads(outputs["dependency_repositories"]) == json.loads(
        dependency_row(git_repo.path, checkout_ref=first)
    )

    (git_repo.path / "later.txt").write_text("later")
    git_repo.commit("second")
    proc, workspace = run_build_dependency_clone(
        repo_root, tmp_path, outputs["dependency_repositories"]
    )

    assert proc.returncode == 0, proc.stderr
    assert not (workspace / "src/index-dependencies/dep_repo/later.txt").exists()


def test_sweep_passes_the_pinned_dependencies_to_the_build(repo_root):
    workflow = yaml.safe_load((repo_root / ".github/workflows/sweep-repository.yaml").read_text())
    assert (
        workflow["jobs"]["resolve"]["outputs"]["dependency_repositories"]
        == "${{ steps.clone.outputs.dependency_repositories }}"
    )
    # The resolve output is empty when the caller sets base_image_stage.
    assert (
        workflow["jobs"]["build"]["with"]["dependency_repositories"]
        == "${{ needs.resolve.outputs.dependency_repositories || inputs.dependency_repositories }}"
    )


@pytest.mark.parametrize("field", ["repository", "ref_value", "checkout_ref"])
def test_build_clone_step_refuses_a_value_that_git_reads_as_an_option(
    repo_root, tmp_path, git_repo, field
):
    source_repository(git_repo, "pkg_a")
    expressions = {
        "inputs.repository": str(git_repo.path),
        "inputs.ref_value": "main",
        "inputs.checkout_ref": "",
        f"inputs.{field}": "--orphan=x",
    }
    env = step_env(workflow_step(repo_root, "Clone repository"), expressions)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    proc = run_step(
        repo_root, workspace, "Clone repository", {**env, "GITHUB_WORKSPACE": str(workspace)}
    )

    assert proc.returncode != 0
    assert "::error::refusing a repository or ref that starts with '-'" in proc.stdout
    assert not (workspace / "src").exists()


@pytest.mark.parametrize("field", ["repository", "ref_value", "checkout_ref"])
def test_build_dependency_clone_refuses_a_value_that_git_reads_as_an_option(
    repo_root, tmp_path, git_repo, field
):
    source_repository(git_repo, "pkg_a")
    dependency = json.loads(dependency_row(git_repo.path))[0]
    dependency[field] = "--orphan=x"

    proc, workspace = run_build_dependency_clone(repo_root, tmp_path, json.dumps([dependency]))

    assert proc.returncode != 0
    assert "::error::refusing a repository or ref that starts with '-'" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not (workspace / "src/index-dependencies").exists()


def test_clone_steps_refuse_a_line_break_in_a_repository(repo_root, tmp_path, git_repo):
    """A line break in a value must not start a second workflow command."""
    source_repository(git_repo, "pkg_a")
    repository = "/nonexistent\n::error::injected"
    message = "::error::refusing a repository or ref with a control character"

    resolve, _, _ = run_resolve_clone(repo_root, tmp_path, repository, "main")
    dependency = {**json.loads(dependency_row(git_repo.path))[0], "repository": repository}
    build_dependency, _ = run_build_dependency_clone(repo_root, tmp_path, json.dumps([dependency]))
    expressions = {
        "inputs.repository": repository,
        "inputs.ref_value": "main",
        "inputs.checkout_ref": "",
    }
    env = step_env(workflow_step(repo_root, "Clone repository"), expressions)
    workspace = tmp_path / "clone-workspace"
    workspace.mkdir()
    build = run_step(
        repo_root, workspace, "Clone repository", {**env, "GITHUB_WORKSPACE": str(workspace)}
    )

    for proc in (resolve, build_dependency, build):
        output = proc.stdout + proc.stderr
        assert proc.returncode != 0
        assert message in output
        assert not [line for line in output.splitlines() if line.startswith("::error::injected")]


@pytest.mark.parametrize("use_checkout_ref", [True, False])
def test_build_clone_step_prefers_the_commit_that_the_detection_read(
    repo_root, tmp_path, git_repo, use_checkout_ref
):
    first = source_repository(git_repo, "pkg_a")
    (git_repo.path / "later.txt").write_text("later")
    second = git_repo.commit("second")
    env = step_env(
        workflow_step(repo_root, "Clone repository"),
        {
            "inputs.repository": str(git_repo.path),
            "inputs.ref_value": "main",
            "inputs.checkout_ref": first if use_checkout_ref else "",
        },
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    proc = run_step(
        repo_root, workspace, "Clone repository", {**env, "GITHUB_WORKSPACE": str(workspace)}
    )

    assert proc.returncode == 0, proc.stderr
    head = subprocess.run(
        ["git", "-C", str(workspace / "src/repository"), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == (first if use_checkout_ref else second)


def test_cache_restores_exact_build_without_fallback_or_rebuild(repo_root):
    save = workflow_step(repo_root, "Cache built workspace")
    restore = workflow_step(repo_root, "Restore built workspace", "test-repository")
    assert save["with"]["path"] == restore["with"]["path"]
    assert {"src", "build", "install", ".index-closures"} == set(save["with"]["path"].split())
    assert restore["with"]["fail-on-cache-miss"] is True
    assert "restore-keys" not in restore["with"]
    handoff = workflow_step(repo_root, "Prepare build handoff")
    for field in ("github.run_id", "github.run_attempt", "inputs.ros_distro", "inputs.repo_name"):
        assert field in handoff["env"]["CACHE_KEY"]
    test_body = (repo_root / ".github/workflows/test-repository.yaml").read_text()
    assert "colcon build" not in test_body
    assert "git clone" not in test_body


def prepare_test_result(repo_root, tmp_path, results, present="pkg_a\npkg_b\n"):
    result = compose(repo_root, tmp_path, "pkg_a pkg_b", results, present)
    directory = tmp_path / "_result"
    directory.mkdir()
    path = directory / "result.json"
    path.write_text(json.dumps(result))
    return path


def test_test_rerun_resets_previous_outcomes_before_cache_restore(repo_root, tmp_path):
    path = prepare_test_result(
        repo_root, tmp_path, "pkg_a success failure\npkg_b failure skipped\n"
    )
    output = tmp_path / "output"
    proc = run_step(
        repo_root,
        tmp_path,
        "Select successfully built packages",
        {"GITHUB_OUTPUT": str(output)},
        workflow_name="test-repository",
    )
    assert proc.returncode == 0, proc.stderr
    assert output.read_text() == "packages=pkg_a\n"
    result = json.loads(path.read_text())
    assert result["packages"]["pkg_a"] == {
        "present": True,
        "build_outcome": "success",
        "test_outcome": None,
    }
    assert statuses(result, "pkg_a pkg_b") == {"pkg_b": "fail"}
    # A cache miss or setup error now leaves a null test outcome, not the
    # previous run's failure. The independent test check must still go red.
    report = run_test_report_step(repo_root, tmp_path)
    assert report.returncode == 1
    assert "inconclusive" in (tmp_path / "summary").read_text()


def run_test_report_step(repo_root, tmp_path, checked_out=True):
    # The job checks out this repository at _index; point it at the work tree.
    if checked_out:
        (tmp_path / "_index").symlink_to(repo_root)
    env = step_env(workflow_step(repo_root, "Report tests", "test-repository"))
    return run_step(
        repo_root,
        tmp_path,
        "Report tests",
        {
            **env,
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "autowarefoundation/autoware-index",
            "GITHUB_RUN_ID": "42",
        },
        workflow_name="test-repository",
    )


def test_test_report_names_the_failed_case(repo_root, tmp_path):
    prepare_test_result(repo_root, tmp_path, "pkg_a success failure\npkg_b success success\n")
    results = tmp_path / "build" / "pkg_a" / "test_results" / "pkg_a"
    results.mkdir(parents=True)
    (results / "test_a.gtest.xml").write_text(
        '<testsuites><testsuite name="A"><testcase classname="A" name="works"/>'
        '<testcase classname="A" name="breaks"><failure message="a.cpp:9&#10;'
        'Expected equality of these values"/></testcase></testsuite></testsuites>'
    )
    proc = run_test_report_step(repo_root, tmp_path)
    assert proc.returncode == 1, proc.stderr
    assert "::error title=pkg_a tests failed::1 of 2 test cases failed: A.breaks" in proc.stdout
    assert "Expected equality of these values" in (tmp_path / "summary").read_text()


@pytest.mark.parametrize("second_test,code", [("failure", 1), ("success", 0)])
def test_test_report_keeps_the_verdict_when_the_scripts_checkout_failed(
    repo_root, tmp_path, second_test, code
):
    prepare_test_result(
        repo_root, tmp_path, f"pkg_a success success\npkg_b success {second_test}\n"
    )
    proc = run_test_report_step(repo_root, tmp_path, checked_out=False)
    assert proc.returncode == code, proc.stderr
    assert "checkout of the index scripts failed" in proc.stdout
    assert ("::error::pkg_b: tests failure" in proc.stdout) == (second_test == "failure")
    assert "::error::pkg_a" not in proc.stdout
    assert f"### test {REPO_NAME}" in (tmp_path / "summary").read_text()


def test_restore_rejects_a_non_exact_cache_hit(repo_root, tmp_path):
    proc = run_step(
        repo_root,
        tmp_path,
        "Verify restored build",
        {"CACHE_HIT": "false"},
        workflow_name="test-repository",
    )
    assert proc.returncode != 0
    assert "exact build cache was not restored" in proc.stdout


def test_restore_rejects_a_different_source_commit(repo_root, tmp_path):
    prepare_test_result(repo_root, tmp_path, "pkg_a success skipped\npkg_b failure skipped\n")
    proc = run_step(
        repo_root,
        tmp_path,
        "Verify restored build",
        {"CACHE_HIT": "true"},
        stubs={"git": 'if [ "$1" = "config" ]; then exit 0; fi; echo wrong-sha'},
        workflow_name="test-repository",
    )
    assert proc.returncode != 0
    assert "differs from build SHA" in proc.stderr


@pytest.mark.parametrize("second_build,expected_calls", [("success", 2), ("failure", 1)])
def test_test_failures_do_not_change_build_or_sibling_verdicts(
    repo_root, tmp_path, second_build, expected_calls
):
    path = prepare_test_result(
        repo_root, tmp_path, f"pkg_a success skipped\npkg_b {second_build} skipped\n"
    )
    # Stub only the image's setup files. The complete workflow body, including
    # real subprocess calls to the stub colcon, is executed unchanged.
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("function .() { :; }\n")
    calls = tmp_path / "colcon-calls"
    proc = run_step(
        repo_root,
        tmp_path,
        "Run package tests",
        {
            "ROS_DISTRO_INPUT": DISTRO,
            "BASH_ENV": str(bash_env),
            "CALLS": str(calls),
            "GITHUB_OUTPUT": str(tmp_path / "output"),
        },
        stubs={"colcon": 'echo "$*" >> "$CALLS"\n[[ "$*" != *"pkg_a"* ]]'},
        workflow_name="test-repository",
    )
    assert proc.returncode == 0, proc.stderr
    assert len(calls.read_text().splitlines()) == expected_calls
    # The test logs upload runs only on this output.
    assert (tmp_path / "output").read_text() == "failed=true\n"
    result = json.loads(path.read_text())
    assert_valid(repo_root, result)
    assert result["packages"]["pkg_a"]["build_outcome"] == "success"
    assert result["packages"]["pkg_a"]["test_outcome"] == "failure"
    assert result["packages"]["pkg_b"]["build_outcome"] == second_build
    assert result["packages"]["pkg_b"]["test_outcome"] == (
        "success" if second_build == "success" else None
    )
    assert statuses(result, "pkg_a pkg_b") == {
        "pkg_a": "fail",
        "pkg_b": "pass" if second_build == "success" else "fail",
    }


def run_report_step(repo_root, tmp_path, checked_out=True):
    # The job checks out this repository at _index; point it at the work tree.
    if checked_out:
        (tmp_path / "_index").symlink_to(repo_root)
    env = step_env(workflow_step(repo_root, "Report"))
    return run_step(
        repo_root,
        tmp_path,
        "Report",
        {
            **env,
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "autowarefoundation/autoware-index",
            "GITHUB_RUN_ID": "42",
        },
    )


def test_build_check_reports_success_without_waiting_for_tests(repo_root, tmp_path):
    (tmp_path / ".sweep-results.txt").write_text("pkg_a success skipped\n")
    (tmp_path / ".sweep-present.txt").write_text("pkg_a\n")
    proc = run_report_step(repo_root, tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_report_step_fails_and_names_the_cause_of_a_build_failure(repo_root, tmp_path):
    (tmp_path / ".sweep-results.txt").write_text("pkg_a failure skipped\n")
    (tmp_path / ".sweep-present.txt").write_text("pkg_a\n")
    (tmp_path / ".sweep-reasons.txt").write_text("pkg_a\tfailed\tpkg_a\n")
    (tmp_path / ".sweep-build.log").write_text(
        "--- output: pkg_a\nsrc/a.cpp:3:1: error: expected ';'\n---\n"
    )
    proc = run_report_step(repo_root, tmp_path)
    assert proc.returncode == 1, proc.stderr
    assert "::error title=pkg_a build failed::src/a.cpp:3:1: error: expected ';'" in proc.stdout
    assert "src/a.cpp:3:1: error: expected ';'" in (tmp_path / "summary").read_text()


def test_report_step_keeps_the_verdicts_when_the_scripts_checkout_failed(repo_root, tmp_path):
    (tmp_path / ".sweep-results.txt").write_text("pkg_a failure skipped\n")
    proc = run_report_step(repo_root, tmp_path, checked_out=False)
    assert proc.returncode == 1
    assert "checkout of the index scripts failed" in proc.stdout
    assert "pkg_a failure skipped" in (tmp_path / "summary").read_text()


@pytest.mark.parametrize(
    "failed,missing,installed,expected",
    [
        ("pkg_a\n", "", {"pkg_dep"}, "pkg_a\tfailed\tpkg_a\n"),
        ("pkg_dep\n", "", {"pkg_a"}, "pkg_a\tfailed\tpkg_dep\n"),
        ("", "pkg_dep\n", {"pkg_a"}, "pkg_a\tmissing\tpkg_dep\n"),
        ("", "", {"pkg_a"}, "pkg_a\tnot-installed\tpkg_dep\n"),
        ("", "", {"pkg_a", "pkg_dep"}, ""),
    ],
)
def test_verdict_step_records_why_each_package_failed(
    repo_root, tmp_path, failed, missing, installed, expected
):
    """The Report step reads the cause from .sweep-reasons.txt, never guesses it."""
    (tmp_path / ".sweep-present.txt").write_text("pkg_a\n")
    (tmp_path / ".sweep-failed.txt").write_text(failed)
    (tmp_path / ".sweep-index-missing.txt").write_text(missing)
    (tmp_path / ".index-closures").mkdir()
    (tmp_path / ".index-closures" / "pkg_a").write_text("pkg_dep\n")
    for pkg in installed:
        (tmp_path / "install" / pkg).mkdir(parents=True)
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("function .() { :; }\n")
    proc = run_step(
        repo_root,
        tmp_path,
        "Derive per-package build verdicts",
        {
            "ROS_DISTRO_INPUT": DISTRO,
            "BASH_ENV": str(bash_env),
            "GITHUB_OUTPUT": str(tmp_path / "output"),
        },
        # `colcon list --names-only --packages-up-to pkg_a ...`: the closure
        # edge to pkg_dep comes from .index-closures, as for exec-only edges.
        stubs={"colcon": 'echo "$4"'},
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / ".sweep-reasons.txt").read_text() == expected
    # The logs upload runs only on this output.
    output = tmp_path / "output"
    assert (output.read_text() if output.exists() else "") == ("failed=true\n" if expected else "")


def test_partial_build_failure_preserves_the_successful_build_handoff(repo_root, tmp_path):
    (tmp_path / ".sweep-results.txt").write_text("pkg_a failure skipped\npkg_b success skipped\n")
    output = tmp_path / "output"
    proc = run_step(
        repo_root,
        tmp_path,
        "Prepare build handoff",
        {"CACHE_KEY": "exact-build-attempt", "GITHUB_OUTPUT": str(output)},
    )
    assert proc.returncode == 0, proc.stderr
    assert output.read_text() == "has-tests=true\ncache-key=exact-build-attempt\n"


@pytest.mark.parametrize("results", [None, "pkg_a failure skipped\n", ""])
def test_no_test_job_when_nothing_built(repo_root, tmp_path, results):
    if results is not None:
        (tmp_path / ".sweep-results.txt").write_text(results)
    output = tmp_path / "output"
    proc = run_step(
        repo_root,
        tmp_path,
        "Prepare build handoff",
        {"CACHE_KEY": "unused", "GITHUB_OUTPUT": str(output)},
    )
    assert proc.returncode == 0, proc.stderr
    assert not output.exists()
