"""Tests for scripts/diff_matrix.py (PR build-check matrix).

The matrix builder diffs the PR's HEAD registry against its merge-base BASE
registry and emits rows only for entries whose sweep tuple (url, ref,
registered package set) the PR added or changed. The scoping semantics are
the whole point — untouched-but-stale entries must never be blamed on the
PR, and metadata-only edits must never burn a container build — so they get
exhaustive coverage here.
"""

import json

import diff_matrix as m
import pytest
from registry_load import RegistryError
import yaml


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def entry(url="https://github.com/example-org/awesome_tools", kind="tag", value="1.2.0", **over):
    spec = {
        "url": url,
        "ref": {"kind": kind, "value": value},
        "governance": "community",
        "maintainers": [{"name": "J", "email": "j@x.dev", "github": "j"}],
        "packages": {
            "autoware_a_filter": {"tags": ["sensing"]},
            "zz_planner_b": {"tags": ["planning"]},
        },
    }
    spec.update(over)
    return spec


def write_distribution(dirpath, distro="jazzy", repositories=None):
    dirpath.mkdir(parents=True, exist_ok=True)
    repos = repositories if repositories is not None else {"awesome_tools": entry()}
    doc = {"schema_version": "2", "ros_distro": distro, "repositories": repos}
    (dirpath / f"{distro}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return dirpath


def base_and_head(tmp_path, base_repos, head_repos, distro="jazzy"):
    base = write_distribution(tmp_path / "base", distro=distro, repositories=base_repos)
    head = write_distribution(tmp_path / "head", distro=distro, repositories=head_repos)
    return base, head


AWESOME_ROW = {
    "ros_distro": "jazzy",
    "repo_name": "awesome_tools",
    "repository": "https://github.com/example-org/awesome_tools",
    "ref_kind": "tag",
    "ref_value": "1.2.0",
    "packages": "autoware_a_filter zz_planner_b",
}


# --------------------------------------------------------------------------
# added / unchanged / removed entries
# --------------------------------------------------------------------------
def test_added_entry_emits_exact_row(tmp_path):
    base, head = base_and_head(tmp_path, {}, {"awesome_tools": entry()})
    assert m.build_matrix(base, head) == [AWESOME_ROW]


def test_identical_registries_emit_nothing(tmp_path):
    base, head = base_and_head(tmp_path, {"awesome_tools": entry()}, {"awesome_tools": entry()})
    assert m.build_matrix(base, head) == []


def test_removed_entry_emits_nothing(tmp_path):
    base, head = base_and_head(tmp_path, {"awesome_tools": entry()}, {})
    assert m.build_matrix(base, head) == []


def test_untouched_sibling_is_never_blamed(tmp_path):
    # The PR adds one entry next to an existing one; only the new entry
    # builds — unlike `sweep_matrix --mode eager`, which would also pick up
    # any sibling whose state cursor happens to be stale.
    old = {"awesome_tools": entry()}
    new = {
        "awesome_tools": entry(),
        "brand_new": entry(url="https://github.com/example-org/brand_new"),
    }
    base, head = base_and_head(tmp_path, old, new)
    assert [r["repo_name"] for r in m.build_matrix(base, head)] == ["brand_new"]


# --------------------------------------------------------------------------
# tuple-change detection (mirrors the sweep diff tuple)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "delta",
    [
        {"value": "1.3.0"},  # ref value bump
        {"kind": "branch", "value": "main"},  # ref kind switch
        {"url": "https://github.com/example-org/other_repo"},  # URL-only change
        {"packages": {"autoware_a_filter": {"tags": ["sensing"]}}},  # package removed
        {
            "packages": {
                "autoware_a_filter": {"tags": ["sensing"]},
                "zz_planner_b": {"tags": ["planning"]},
                "extra_pkg": {"tags": ["tool"]},
            }
        },  # package added
    ],
)
def test_any_tuple_change_rebuilds(tmp_path, delta):
    base, head = base_and_head(
        tmp_path, {"awesome_tools": entry()}, {"awesome_tools": entry(**delta)}
    )
    assert [r["repo_name"] for r in m.build_matrix(base, head)] == ["awesome_tools"]


def test_metadata_only_changes_do_not_rebuild(tmp_path):
    # tags/description/maintainers/governance are outside the diffed tuple:
    # editing them must not burn a container build.
    changed = entry(
        governance="foundation",
        maintainers=[{"name": "New", "email": "new@x.dev", "github": "new"}],
        packages={
            "autoware_a_filter": {"tags": ["sensing", "tool"], "description": "new text"},
            "zz_planner_b": {"tags": ["planning"]},
        },
    )
    base, head = base_and_head(tmp_path, {"awesome_tools": entry()}, {"awesome_tools": changed})
    assert m.build_matrix(base, head) == []


def test_same_name_in_new_distro_counts_as_added(tmp_path):
    # Entries are keyed by (distro, repo_name): registering an existing repo
    # into a second distro file is a new registration there.
    base = write_distribution(tmp_path / "base", distro="jazzy")
    head = tmp_path / "head"
    write_distribution(head, distro="jazzy")
    write_distribution(head, distro="humble")
    rows = m.build_matrix(base, head)
    assert [(r["ros_distro"], r["repo_name"]) for r in rows] == [("humble", "awesome_tools")]


def test_fixing_a_malformed_base_entry_rebuilds(tmp_path):
    # The base tuple is kept partial rather than dropped, so the fixed head
    # entry compares unequal and gets built.
    broken = {"url": "https://github.com/example-org/awesome_tools"}
    base, head = base_and_head(tmp_path, {"awesome_tools": broken}, {"awesome_tools": entry()})
    assert [r["repo_name"] for r in m.build_matrix(base, head)] == ["awesome_tools"]


# --------------------------------------------------------------------------
# row shape + ordering + degenerate inputs
# --------------------------------------------------------------------------
def test_rows_sorted_by_distro_then_repo(tmp_path):
    base = tmp_path / "base"
    write_distribution(base, distro="jazzy", repositories={})
    write_distribution(base, distro="humble", repositories={})
    head = tmp_path / "head"
    write_distribution(head, distro="jazzy", repositories={"zzz": entry(), "aaa": entry()})
    write_distribution(head, distro="humble", repositories={"mmm": entry()})
    rows = m.build_matrix(base, head)
    assert [(r["ros_distro"], r["repo_name"]) for r in rows] == [
        ("humble", "mmm"),
        ("jazzy", "aaa"),
        ("jazzy", "zzz"),
    ]


def test_packages_space_joined_sorted(tmp_path):
    repos = {"r": entry(packages={"zzz_pkg": {"tags": ["a"]}, "aaa_pkg": {"tags": ["b"]}})}
    base, head = base_and_head(tmp_path, {}, repos)
    assert m.build_matrix(base, head)[0]["packages"] == "aaa_pkg zzz_pkg"


def test_missing_base_dir_fails_loudly(tmp_path):
    # A miswired checkout path must not silently rebuild the whole registry.
    head = write_distribution(tmp_path / "head")
    with pytest.raises(RegistryError) as exc:
        m.build_matrix(tmp_path / "nope", head)
    assert "base distributions dir not found" in str(exc.value)


def test_malformed_head_entries_fail_loudly(tmp_path):
    repos = {
        "no_url": {"ref": {"kind": "tag", "value": "1"}, "packages": {"p": {"tags": ["a"]}}},
        "no_packages": {"url": "u", "ref": {"kind": "tag", "value": "1"}, "packages": {}},
        "good": entry(),
    }
    base, head = base_and_head(tmp_path, {}, repos)
    with pytest.raises(RegistryError) as exc:
        m.build_matrix(base, head)
    assert "no_url" in str(exc.value) and "no_packages" in str(exc.value)
    assert "unbuildable" in str(exc.value)


def test_v1_head_file_raises_registry_error(tmp_path):
    base = write_distribution(tmp_path / "base", repositories={})
    head = tmp_path / "head"
    head.mkdir()
    (head / "jazzy.yaml").write_text('schema_version: "1"\nros_distro: jazzy\npackages: {}\n')
    with pytest.raises(RegistryError):
        m.build_matrix(base, head)


def test_v1_base_file_raises_registry_error(tmp_path):
    # An unloadable base means added-vs-changed cannot be told apart; a hard
    # failure beats a silently wrong matrix (schema migrations touch this).
    base = tmp_path / "base"
    base.mkdir()
    (base / "jazzy.yaml").write_text('schema_version: "1"\nros_distro: jazzy\npackages: {}\n')
    head = write_distribution(tmp_path / "head")
    with pytest.raises(RegistryError):
        m.build_matrix(base, head)


# --------------------------------------------------------------------------
# main() — CLI surface, max-rows guard, output modes
# --------------------------------------------------------------------------
def run_main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["diff_matrix.py", *argv])
    m.main()


def test_main_writes_output_file(tmp_path, monkeypatch):
    base, head = base_and_head(tmp_path, {}, {"awesome_tools": entry()})
    out = tmp_path / "matrix.json"
    run_main(
        monkeypatch,
        ["--base-dir", str(base), "--head-dir", str(head), "--output", str(out)],
    )
    payload = json.loads(out.read_text())
    assert payload["include"] == [AWESOME_ROW]


def test_main_stdout(tmp_path, monkeypatch, capsys):
    base, head = base_and_head(tmp_path, {}, {"awesome_tools": entry()})
    run_main(monkeypatch, ["--base-dir", str(base), "--head-dir", str(head)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["include"][0]["repo_name"] == "awesome_tools"


def test_main_empty_matrix_is_valid_json(tmp_path, monkeypatch, capsys):
    base, head = base_and_head(tmp_path, {"awesome_tools": entry()}, {"awesome_tools": entry()})
    run_main(monkeypatch, ["--base-dir", str(base), "--head-dir", str(head)])
    assert json.loads(capsys.readouterr().out) == {"include": []}


def test_main_max_rows_guard_fails_loudly(tmp_path, monkeypatch):
    repos = {f"repo_{i}": entry() for i in range(3)}
    base, head = base_and_head(tmp_path, {}, repos)
    with pytest.raises(SystemExit) as exc:
        run_main(
            monkeypatch,
            ["--base-dir", str(base), "--head-dir", str(head), "--max-rows", "2"],
        )
    assert "3 rows" in str(exc.value)
    assert "::error::" in str(exc.value)


def test_main_missing_base_dir_exits_with_error(tmp_path, monkeypatch):
    head = write_distribution(tmp_path / "head")
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, ["--base-dir", str(tmp_path / "nope"), "--head-dir", str(head)])
    assert "::error::" in str(exc.value)


@pytest.mark.parametrize(
    "spec",
    [
        {"url": "u", "ref": "main", "packages": {"p": {"tags": ["a"]}}},  # ref not a mapping
        {"url": "u", "ref": {"kind": "tag", "value": "1"}, "packages": ["p1", "p2"]},  # list
        "just-a-string",  # whole entry not a mapping
    ],
)
def test_main_schema_invalid_shapes_exit_annotated(tmp_path, monkeypatch, spec):
    # Unlike sweep_matrix (fed post-gate main), this script sees raw PR YAML:
    # type garbage where the schema promises mappings must still exit through
    # an ::error:: annotation, never a bare traceback.
    base, head = base_and_head(tmp_path, {}, {"broken": spec})
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, ["--base-dir", str(base), "--head-dir", str(head)])
    assert "::error::" in str(exc.value)
    assert "schema-invalid shape" in str(exc.value)
