"""Tests for scripts/sweep_matrix.py (level-triggered repository sweep matrix).

The matrix builder diffs the registry's DESIRED per-repository state
(url, ref, registered package set) against the LAST CONCLUSIVELY RECORDED
state (data:state/<distro>/<repo>.json) — eager sweeps rows that differ,
nightly sweeps every branch repo plus the same diff as catch-up. The diff
semantics are what make lost sweeps self-healing, so they get exhaustive
coverage here.
"""

import json

import pytest

import sweep_matrix as m
from registry_load import RegistryError


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def write_distribution(dirpath, distro="jazzy", repositories=None):
    dirpath.mkdir(parents=True, exist_ok=True)
    repos = repositories if repositories is not None else {
        "awesome_tools": {
            "url": "https://github.com/example-org/awesome_tools",
            "ref": {"kind": "tag", "value": "1.2.0"},
            "governance": "community",
            "maintainers": [{"name": "J", "email": "j@x.dev", "github": "j"}],
            "packages": {"autoware_a_filter": {"tags": ["sensing"]}, "zz_planner_b": {"tags": ["planning"]}},
        }
    }
    doc = {"schema_version": "2", "ros_distro": distro, "repositories": repos}
    import yaml

    (dirpath / f"{distro}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return dirpath


def write_state(state_dir, distro, repo_name, url, kind, value, packages):
    target = state_dir / distro
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{repo_name}.json").write_text(
        json.dumps(
            {
                "url": url,
                "ref": {"kind": kind, "value": value},
                "packages": packages,
                "last_run_url": "https://example.com/run/1",
                "at": "2026-06-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


MATCHING_STATE = dict(
    url="https://github.com/example-org/awesome_tools",
    kind="tag",
    value="1.2.0",
    packages=["autoware_a_filter", "zz_planner_b"],
)


# --------------------------------------------------------------------------
# recorded_state parsing
# --------------------------------------------------------------------------
def test_recorded_state_missing_file_is_none(tmp_path):
    assert m.recorded_state(tmp_path, "jazzy", "nope") is None


def test_recorded_state_bad_json_is_none(tmp_path):
    (tmp_path / "jazzy").mkdir(parents=True)
    (tmp_path / "jazzy" / "r.json").write_text("{not json", encoding="utf-8")
    assert m.recorded_state(tmp_path, "jazzy", "r") is None


def test_recorded_state_non_dict_is_none(tmp_path):
    (tmp_path / "jazzy").mkdir(parents=True)
    (tmp_path / "jazzy" / "r.json").write_text("[1, 2]", encoding="utf-8")
    assert m.recorded_state(tmp_path, "jazzy", "r") is None


def test_recorded_state_sorts_packages(tmp_path):
    write_state(tmp_path, "jazzy", "r", "u", "tag", "1", ["zz", "aa"])
    assert m.recorded_state(tmp_path, "jazzy", "r")["packages"] == ["aa", "zz"]


# --------------------------------------------------------------------------
# eager mode — sweep iff (url, ref, package set) differs from state
# --------------------------------------------------------------------------
def test_eager_no_state_sweeps_everything(tmp_path):
    dist = write_distribution(tmp_path / "distributions")
    rows = m.build_matrix(dist, tmp_path / "state", "eager")
    assert len(rows) == 1
    assert rows[0] == {
        "ros_distro": "jazzy",
        "repo_name": "awesome_tools",
        "repository": "https://github.com/example-org/awesome_tools",
        "ref_kind": "tag",
        "ref_value": "1.2.0",
        "packages": "autoware_a_filter zz_planner_b",
    }


def test_eager_matching_state_sweeps_nothing(tmp_path):
    dist = write_distribution(tmp_path / "distributions")
    state = tmp_path / "state"
    write_state(state, "jazzy", "awesome_tools", **MATCHING_STATE)
    assert m.build_matrix(dist, state, "eager") == []


@pytest.mark.parametrize(
    "delta",
    [
        dict(value="1.3.0"),                                       # ref value bump
        dict(kind="branch", value="main"),                         # ref kind switch
        dict(url="https://github.com/example-org/other_repo"),     # URL-only change
        dict(packages=["autoware_a_filter"]),                      # package added since
        dict(packages=["autoware_a_filter", "zz_planner_b", "x"]), # package removed since
    ],
)
def test_eager_any_tuple_change_resweeps(tmp_path, delta):
    # The state file records something OTHER than what the registry now says
    # — including a URL-only change, which the old ref-diff missed entirely.
    dist = write_distribution(tmp_path / "distributions")
    state = tmp_path / "state"
    write_state(state, "jazzy", "awesome_tools", **{**MATCHING_STATE, **delta})
    rows = m.build_matrix(dist, state, "eager")
    assert [r["repo_name"] for r in rows] == ["awesome_tools"]


def test_eager_state_package_order_is_insensitive(tmp_path):
    dist = write_distribution(tmp_path / "distributions")
    state = tmp_path / "state"
    write_state(
        state, "jazzy", "awesome_tools",
        **{**MATCHING_STATE, "packages": ["zz_planner_b", "autoware_a_filter"]},
    )
    assert m.build_matrix(dist, state, "eager") == []


def test_eager_metadata_only_changes_do_not_trigger(tmp_path):
    # tags/description/maintainers are not part of the diffed tuple: editing
    # them must not burn a sweep.
    repos = {
        "awesome_tools": {
            "url": "https://github.com/example-org/awesome_tools",
            "ref": {"kind": "tag", "value": "1.2.0"},
            "governance": "foundation",  # changed
            "maintainers": [{"name": "New", "email": "new@x.dev", "github": "new"}],  # changed
            "packages": {
                "autoware_a_filter": {"tags": ["sensing", "extra"], "description": "new text"},
                "zz_planner_b": {"tags": ["planning"]},
            },
        }
    }
    dist = write_distribution(tmp_path / "distributions", repositories=repos)
    state = tmp_path / "state"
    write_state(state, "jazzy", "awesome_tools", **MATCHING_STATE)
    assert m.build_matrix(dist, state, "eager") == []


# --------------------------------------------------------------------------
# nightly mode — every branch repo, plus the state-diff as catch-up
# --------------------------------------------------------------------------
def branch_repo(packages=None):
    return {
        "url": "https://github.com/example-org/rolling_repo",
        "ref": {"kind": "branch", "value": "main"},
        "governance": "community",
        "maintainers": [{"name": "J", "email": "j@x.dev", "github": "j"}],
        "packages": packages or {"rolling_pkg": {"tags": ["sensing"]}},
    }


def test_nightly_branch_repo_always_swept_even_with_fresh_state(tmp_path):
    dist = write_distribution(tmp_path / "distributions", repositories={"rolling_repo": branch_repo()})
    state = tmp_path / "state"
    write_state(
        state, "jazzy", "rolling_repo",
        url="https://github.com/example-org/rolling_repo", kind="branch", value="main",
        packages=["rolling_pkg"],
    )
    rows = m.build_matrix(dist, state, "nightly")
    assert [r["repo_name"] for r in rows] == ["rolling_repo"]


def test_nightly_pinned_repo_with_fresh_state_not_swept(tmp_path):
    dist = write_distribution(tmp_path / "distributions")
    state = tmp_path / "state"
    write_state(state, "jazzy", "awesome_tools", **MATCHING_STATE)
    assert m.build_matrix(dist, state, "nightly") == []


def test_nightly_pinned_repo_with_stale_state_caught_up(tmp_path):
    # An eager run lost to cancellation left the tag repo unrecorded; nightly
    # picks it up via the same state-diff.
    dist = write_distribution(tmp_path / "distributions")
    rows = m.build_matrix(dist, tmp_path / "state", "nightly")
    assert [r["repo_name"] for r in rows] == ["awesome_tools"]


def test_nightly_branch_and_stale_is_one_row(tmp_path):
    dist = write_distribution(tmp_path / "distributions", repositories={"rolling_repo": branch_repo()})
    rows = m.build_matrix(dist, tmp_path / "state", "nightly")
    assert len(rows) == 1


# --------------------------------------------------------------------------
# row shape + ordering + degenerate entries
# --------------------------------------------------------------------------
def test_rows_sorted_by_distro_then_repo(tmp_path):
    dist = tmp_path / "distributions"
    write_distribution(dist, distro="jazzy", repositories={"zzz": branch_repo(), "aaa": branch_repo()})
    # Same-named repos in another distro file.
    write_distribution(dist, distro="humble", repositories={"mmm": branch_repo()})
    rows = m.build_matrix(dist, tmp_path / "state", "nightly")
    assert [(r["ros_distro"], r["repo_name"]) for r in rows] == [
        ("humble", "mmm"),
        ("jazzy", "aaa"),
        ("jazzy", "zzz"),
    ]


def test_packages_space_joined_sorted(tmp_path):
    repos = {"r": branch_repo(packages={"zzz_pkg": {"tags": ["a"]}, "aaa_pkg": {"tags": ["b"]}})}
    dist = write_distribution(tmp_path / "distributions", repositories=repos)
    rows = m.build_matrix(dist, tmp_path / "state", "nightly")
    assert rows[0]["packages"] == "aaa_pkg zzz_pkg"


def test_missing_fields_fail_discover_loudly(tmp_path):
    # A schema-valid file can't produce these (url/ref/packages are required);
    # reaching build_matrix with one means the PR gate was bypassed. Soft-
    # skipping would leave the entry registered-but-never-swept with all CI
    # green, so discover must go red, naming every offender.
    repos = {
        "no_url": {"ref": {"kind": "tag", "value": "1"}, "packages": {"p": {"tags": ["a"]}}},
        "no_packages": {"url": "u", "ref": {"kind": "tag", "value": "1"}, "packages": {}},
        "good": branch_repo(),
    }
    dist = write_distribution(tmp_path / "distributions", repositories=repos)
    with pytest.raises(RegistryError) as exc:
        m.build_matrix(dist, tmp_path / "state", "eager")
    assert "no_url" in str(exc.value) and "no_packages" in str(exc.value)
    assert "unsweepable" in str(exc.value)


def test_v1_distribution_raises_registry_error(tmp_path):
    dist = tmp_path / "distributions"
    dist.mkdir()
    (dist / "jazzy.yaml").write_text('schema_version: "1"\nros_distro: jazzy\npackages: {}\n')
    with pytest.raises(RegistryError):
        m.build_matrix(dist, tmp_path / "state", "eager")


# --------------------------------------------------------------------------
# main() — CLI surface, max-rows guard, output modes
# --------------------------------------------------------------------------
def run_main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["sweep_matrix.py", *argv])
    m.main()


def test_main_writes_output_file(tmp_path, monkeypatch):
    dist = write_distribution(tmp_path / "distributions")
    out = tmp_path / "matrix.json"
    run_main(
        monkeypatch,
        ["--mode", "eager", "--distributions-dir", str(dist), "--state-dir", str(tmp_path / "state"), "--output", str(out)],
    )
    payload = json.loads(out.read_text())
    assert len(payload["include"]) == 1


def test_main_stdout(tmp_path, monkeypatch, capsys):
    dist = write_distribution(tmp_path / "distributions")
    run_main(
        monkeypatch,
        ["--mode", "eager", "--distributions-dir", str(dist), "--state-dir", str(tmp_path / "state")],
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["include"][0]["repo_name"] == "awesome_tools"


def test_main_max_rows_guard_fails_loudly(tmp_path, monkeypatch):
    repos = {f"repo_{i}": branch_repo() for i in range(3)}
    dist = write_distribution(tmp_path / "distributions", repositories=repos)
    with pytest.raises(SystemExit) as exc:
        run_main(
            monkeypatch,
            ["--mode", "nightly", "--distributions-dir", str(dist), "--state-dir", str(tmp_path / "state"), "--max-rows", "2"],
        )
    assert "3 rows" in str(exc.value)
    assert "::error::" in str(exc.value)


def test_main_v1_file_exits_with_error(tmp_path, monkeypatch):
    dist = tmp_path / "distributions"
    dist.mkdir()
    (dist / "jazzy.yaml").write_text('schema_version: "1"\nros_distro: jazzy\n')
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, ["--mode", "eager", "--distributions-dir", str(dist), "--state-dir", str(tmp_path / "s")])
    assert "not supported" in str(exc.value)
