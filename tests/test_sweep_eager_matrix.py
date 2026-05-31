"""Tests for scripts/sweep_eager_matrix.py.

These functions shell out to git against the current working dir, so each test
monkeypatches the process cwd into the throwaway git_repo. We build real
distribution YAML files, commit them, mutate a ref, commit again, and assert
on what the diff-driven matrix builder reports.
"""

import pytest
import yaml

import sweep_eager_matrix as sem

ZERO_SHA = "0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _distro_yaml(ros_distro="jazzy", packages=None):
    """Render a distribution YAML document as text."""
    doc = {"schema_version": "1", "ros_distro": ros_distro}
    if packages is not None:
        doc["packages"] = packages
    return yaml.safe_dump(doc, sort_keys=False)


def _pkg(repository, kind, value, **extra):
    spec = {"repository": repository, "ref": {"kind": kind, "value": value}}
    spec.update(extra)
    return spec


def _write(repo, rel_path, text):
    p = repo.path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def chdir_repo(git_repo, monkeypatch):
    """git_repo with the process cwd pointed at it (git show/ls-tree use cwd)."""
    monkeypatch.chdir(git_repo.path)
    return git_repo


# ---------------------------------------------------------------------------
# module-level constants / smoke
# ---------------------------------------------------------------------------


def test_zero_sha_constant():
    assert sem.ZERO_SHA == ZERO_SHA
    assert len(sem.ZERO_SHA) == 40
    assert set(sem.ZERO_SHA) == {"0"}


def test_distributions_dir_constant():
    assert str(sem.DISTRIBUTIONS_DIR) == "distributions"


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


def test_run_returns_completed_process_with_text():
    result = sem.run(["git", "--version"])
    assert result.returncode == 0
    assert "git version" in result.stdout
    # text=True => stdout is str, not bytes
    assert isinstance(result.stdout, str)


def test_run_check_false_does_not_raise(chdir_repo):
    # Unknown rev: with check=False the failing process is returned, not raised.
    result = sem.run(["git", "rev-parse", "definitely-not-a-ref"], check=False)
    assert result.returncode != 0


def test_run_check_true_raises_on_failure(chdir_repo):
    import subprocess

    with pytest.raises(subprocess.CalledProcessError):
        sem.run(["git", "rev-parse", "definitely-not-a-ref"], check=True)


# ---------------------------------------------------------------------------
# load_yaml_at()
# ---------------------------------------------------------------------------


def test_load_yaml_at_reads_committed_file(chdir_repo):
    pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages=pkgs))
    sha = chdir_repo.commit("add jazzy")

    doc = sem.load_yaml_at(sha, "distributions/jazzy.yaml")
    assert doc["ros_distro"] == "jazzy"
    assert doc["packages"]["pkg_a"]["ref"] == {"kind": "tag", "value": "1.0.0"}


def test_load_yaml_at_missing_path_returns_none(chdir_repo):
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages={}))
    sha = chdir_repo.commit("c")

    assert sem.load_yaml_at(sha, "distributions/nope.yaml") is None


def test_load_yaml_at_unknown_sha_returns_none(chdir_repo):
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages={}))
    chdir_repo.commit("c")

    assert sem.load_yaml_at("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                            "distributions/jazzy.yaml") is None


# ---------------------------------------------------------------------------
# list_distribution_files()
# ---------------------------------------------------------------------------


def test_list_distribution_files_only_yaml(chdir_repo):
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages={}))
    _write(chdir_repo, "distributions/humble.yaml",
           _distro_yaml(ros_distro="humble", packages={}))
    _write(chdir_repo, "distributions/README.md", "# notes\n")
    _write(chdir_repo, "distributions/notes.txt", "ignore me\n")
    _write(chdir_repo, "other/thing.yaml", _distro_yaml(packages={}))
    sha = chdir_repo.commit("c")

    files = sem.list_distribution_files(sha)
    assert sorted(files) == ["distributions/humble.yaml", "distributions/jazzy.yaml"]
    # nothing outside distributions/, no non-yaml
    assert all(f.startswith("distributions/") and f.endswith(".yaml") for f in files)


def test_list_distribution_files_empty_when_none(chdir_repo):
    _write(chdir_repo, "README.md", "# repo\n")
    sha = chdir_repo.commit("c")
    assert sem.list_distribution_files(sha) == []


# ---------------------------------------------------------------------------
# packages_with_changed_ref() — PRIORITY
# ---------------------------------------------------------------------------


def _commit_two_revs(repo, before_packages, after_packages,
                     before_distro="jazzy", after_distro="jazzy",
                     path="distributions/jazzy.yaml"):
    """Commit before-state, then after-state; return (before_sha, after_sha)."""
    _write(repo, path, _distro_yaml(ros_distro=before_distro, packages=before_packages))
    before = repo.commit("before")
    _write(repo, path, _distro_yaml(ros_distro=after_distro, packages=after_packages))
    after = repo.commit("after")
    return before, after


def test_changed_ref_value_detected(chdir_repo):
    before_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    after_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.1.0")}
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    changed = sem.packages_with_changed_ref(before, after)
    assert changed == [("distributions/jazzy.yaml", "pkg_a")]


def test_changed_ref_kind_detected(chdir_repo):
    before_pkgs = {"pkg_a": _pkg("https://example.com/a", "branch", "main")}
    after_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "main")}
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    changed = sem.packages_with_changed_ref(before, after)
    assert changed == [("distributions/jazzy.yaml", "pkg_a")]


def test_non_ref_field_change_is_ignored(chdir_repo):
    # Only the description changes; ref stays identical -> not "changed".
    before_pkgs = {
        "pkg_a": _pkg("https://example.com/a", "tag", "1.0.0", description="old"),
    }
    after_pkgs = {
        "pkg_a": _pkg("https://example.com/a", "tag", "1.0.0", description="new"),
    }
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    assert sem.packages_with_changed_ref(before, after) == []


def test_repository_change_alone_is_ignored(chdir_repo):
    # repository changes but ref is unchanged -> packages_with_changed_ref ignores it.
    before_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    after_pkgs = {"pkg_a": _pkg("https://example.com/A-MOVED", "tag", "1.0.0")}
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    assert sem.packages_with_changed_ref(before, after) == []


def test_only_changed_package_among_many(chdir_repo):
    before_pkgs = {
        "pkg_a": _pkg("https://example.com/a", "tag", "1.0.0"),
        "pkg_b": _pkg("https://example.com/b", "branch", "main"),
        "pkg_c": _pkg("https://example.com/c", "tag", "2.0.0"),
    }
    after_pkgs = {
        "pkg_a": _pkg("https://example.com/a", "tag", "1.0.0"),
        "pkg_b": _pkg("https://example.com/b", "branch", "develop"),  # changed
        "pkg_c": _pkg("https://example.com/c", "tag", "2.0.0"),
    }
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    assert sem.packages_with_changed_ref(before, after) == [
        ("distributions/jazzy.yaml", "pkg_b"),
    ]


def test_newly_added_package_is_changed(chdir_repo):
    before_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    after_pkgs = {
        "pkg_a": _pkg("https://example.com/a", "tag", "1.0.0"),
        "pkg_new": _pkg("https://example.com/new", "tag", "0.1.0"),
    }
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    # pkg_a unchanged; pkg_new is new (before_ref None != after_ref) -> changed.
    assert sem.packages_with_changed_ref(before, after) == [
        ("distributions/jazzy.yaml", "pkg_new"),
    ]


def test_removed_package_not_reported(chdir_repo):
    # Iteration is over after_files' packages, so a removed package can't appear.
    before_pkgs = {
        "pkg_a": _pkg("https://example.com/a", "tag", "1.0.0"),
        "pkg_gone": _pkg("https://example.com/gone", "tag", "9.9.9"),
    }
    after_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    assert sem.packages_with_changed_ref(before, after) == []


def test_no_changes_returns_empty(chdir_repo):
    pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages=pkgs))
    before = chdir_repo.commit("before")
    # An unrelated file change so the second commit is non-empty, but the
    # distribution file (and its refs) is untouched.
    _write(chdir_repo, "README.md", "# touched\n")
    after = chdir_repo.commit("after")
    assert sem.packages_with_changed_ref(before, after) == []


def test_zero_sha_before_marks_every_package_changed(chdir_repo):
    pkgs = {
        "pkg_a": _pkg("https://example.com/a", "tag", "1.0.0"),
        "pkg_b": _pkg("https://example.com/b", "branch", "main"),
    }
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages=pkgs))
    after = chdir_repo.commit("first")

    changed = sem.packages_with_changed_ref(ZERO_SHA, after)
    assert sorted(changed) == [
        ("distributions/jazzy.yaml", "pkg_a"),
        ("distributions/jazzy.yaml", "pkg_b"),
    ]


def test_zero_sha_before_with_multiple_files(chdir_repo):
    _write(chdir_repo, "distributions/jazzy.yaml",
           _distro_yaml(packages={"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}))
    _write(chdir_repo, "distributions/humble.yaml",
           _distro_yaml(ros_distro="humble",
                        packages={"pkg_h": _pkg("https://example.com/h", "tag", "2.0.0")}))
    after = chdir_repo.commit("first")

    changed = sem.packages_with_changed_ref(ZERO_SHA, after)
    assert sorted(changed) == [
        ("distributions/humble.yaml", "pkg_h"),
        ("distributions/jazzy.yaml", "pkg_a"),
    ]


def test_zero_sha_before_empty_packages(chdir_repo):
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages={}))
    after = chdir_repo.commit("first")
    assert sem.packages_with_changed_ref(ZERO_SHA, after) == []


def test_newly_added_distribution_file(chdir_repo):
    # before: only jazzy. after: jazzy unchanged + brand new humble file.
    _write(chdir_repo, "distributions/jazzy.yaml",
           _distro_yaml(packages={"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}))
    before = chdir_repo.commit("before")
    _write(chdir_repo, "distributions/humble.yaml",
           _distro_yaml(ros_distro="humble",
                        packages={"pkg_h": _pkg("https://example.com/h", "tag", "2.0.0")}))
    after = chdir_repo.commit("after")

    # jazzy/pkg_a unchanged; the whole new file's package counts as changed
    # (before_doc is None -> before_ref None).
    assert sem.packages_with_changed_ref(before, after) == [
        ("distributions/humble.yaml", "pkg_h"),
    ]


def test_file_without_packages_key(chdir_repo):
    # after file has no 'packages' key at all -> after_pkgs == {} -> nothing.
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages=None))
    before = chdir_repo.commit("before")
    _write(chdir_repo, "distributions/jazzy.yaml",
           _distro_yaml(packages=None) + "# touched\n")
    after = chdir_repo.commit("after")

    assert sem.packages_with_changed_ref(before, after) == []


# ---------------------------------------------------------------------------
# build_matrix() — PRIORITY
# ---------------------------------------------------------------------------


def test_build_matrix_single_changed_row(chdir_repo):
    before_pkgs = {
        "autoware_livox_tag_filter": _pkg(
            "https://github.com/autowarefoundation/autoware_livox_tag_filter",
            "branch", "main",
        ),
    }
    after_pkgs = {
        "autoware_livox_tag_filter": _pkg(
            "https://github.com/autowarefoundation/autoware_livox_tag_filter",
            "tag", "0.2.1",
        ),
    }
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    rows = sem.build_matrix(before, after)
    assert rows == [
        {
            "ros_distro": "jazzy",
            "package_name": "autoware_livox_tag_filter",
            "package_repository":
                "https://github.com/autowarefoundation/autoware_livox_tag_filter",
            "ref_kind": "tag",
            "ref_value": "0.2.1",
        }
    ]


def test_build_matrix_row_keys_exact(chdir_repo):
    before_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    after_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.1.0")}
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    [row] = sem.build_matrix(before, after)
    assert set(row.keys()) == {
        "ros_distro",
        "package_name",
        "package_repository",
        "ref_kind",
        "ref_value",
    }


def test_build_matrix_one_row_per_changed_package(chdir_repo):
    before_pkgs = {
        "pkg_a": _pkg("https://example.com/a", "tag", "1.0.0"),
        "pkg_b": _pkg("https://example.com/b", "tag", "1.0.0"),
    }
    after_pkgs = {
        "pkg_a": _pkg("https://example.com/a", "tag", "2.0.0"),  # changed
        "pkg_b": _pkg("https://example.com/b", "tag", "3.0.0"),  # changed
    }
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    rows = sem.build_matrix(before, after)
    by_name = {r["package_name"]: r for r in rows}
    assert set(by_name) == {"pkg_a", "pkg_b"}
    assert by_name["pkg_a"]["ref_value"] == "2.0.0"
    assert by_name["pkg_b"]["ref_value"] == "3.0.0"
    assert all(r["ros_distro"] == "jazzy" for r in rows)


def test_build_matrix_empty_when_no_changes(chdir_repo):
    pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages=pkgs))
    before = chdir_repo.commit("before")
    _write(chdir_repo, "README.md", "# touched\n")
    after = chdir_repo.commit("after")
    assert sem.build_matrix(before, after) == []


def test_build_matrix_skips_row_missing_fields(chdir_repo, capsys):
    # Changed package whose after-spec lacks a repository -> skipped, warning emitted.
    before_pkgs = {"pkg_a": {"ref": {"kind": "tag", "value": "1.0.0"}}}
    after_pkgs = {"pkg_a": {"ref": {"kind": "tag", "value": "2.0.0"}}}  # no repository
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    rows = sem.build_matrix(before, after)
    assert rows == []
    err = capsys.readouterr().err
    assert "skipping" in err
    assert "pkg_a" in err


def test_build_matrix_zero_sha_full_fanout(chdir_repo):
    pkgs = {
        "pkg_a": _pkg("https://example.com/a", "tag", "1.0.0"),
        "pkg_b": _pkg("https://example.com/b", "branch", "main"),
    }
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages=pkgs))
    after = chdir_repo.commit("first")

    rows = sem.build_matrix(ZERO_SHA, after)
    by_name = {r["package_name"]: r for r in rows}
    assert set(by_name) == {"pkg_a", "pkg_b"}
    assert by_name["pkg_a"]["ref_kind"] == "tag"
    assert by_name["pkg_a"]["ref_value"] == "1.0.0"
    assert by_name["pkg_b"]["ref_kind"] == "branch"
    assert by_name["pkg_b"]["ref_value"] == "main"


def test_build_matrix_mixed_distros(chdir_repo):
    _write(chdir_repo, "distributions/jazzy.yaml",
           _distro_yaml(packages={"pkg_j": _pkg("https://example.com/j", "tag", "1.0.0")}))
    _write(chdir_repo, "distributions/humble.yaml",
           _distro_yaml(ros_distro="humble",
                        packages={"pkg_h": _pkg("https://example.com/h", "tag", "1.0.0")}))
    before = chdir_repo.commit("before")
    _write(chdir_repo, "distributions/jazzy.yaml",
           _distro_yaml(packages={"pkg_j": _pkg("https://example.com/j", "tag", "2.0.0")}))
    _write(chdir_repo, "distributions/humble.yaml",
           _distro_yaml(ros_distro="humble",
                        packages={"pkg_h": _pkg("https://example.com/h", "tag", "2.0.0")}))
    after = chdir_repo.commit("after")

    rows = sem.build_matrix(before, after)
    by_distro = {r["ros_distro"]: r for r in rows}
    assert set(by_distro) == {"jazzy", "humble"}
    assert by_distro["jazzy"]["package_name"] == "pkg_j"
    assert by_distro["humble"]["package_name"] == "pkg_h"


# ---------------------------------------------------------------------------
# main() / CLI end-to-end
# ---------------------------------------------------------------------------


def test_main_stdout(chdir_repo, monkeypatch, capsys):
    before_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    after_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "2.0.0")}
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    monkeypatch.setattr(
        sem.sys, "argv",
        ["sweep_eager_matrix.py", "--before", before, "--after", after],
    )
    sem.main()

    out = capsys.readouterr().out
    import json

    payload = json.loads(out)
    assert payload == {
        "include": [
            {
                "ros_distro": "jazzy",
                "package_name": "pkg_a",
                "package_repository": "https://example.com/a",
                "ref_kind": "tag",
                "ref_value": "2.0.0",
            }
        ]
    }


def test_main_compact_json_no_spaces(chdir_repo, monkeypatch, capsys):
    pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    _write(chdir_repo, "distributions/jazzy.yaml", _distro_yaml(packages=pkgs))
    before = chdir_repo.commit("before")
    _write(chdir_repo, "README.md", "# touched\n")
    after = chdir_repo.commit("after")

    monkeypatch.setattr(
        sem.sys, "argv",
        ["sweep_eager_matrix.py", "--before", before, "--after", after],
    )
    sem.main()
    out = capsys.readouterr().out
    # separators=(",", ":") => no spaces after separators
    assert out == '{"include":[]}'


def test_main_output_file(chdir_repo, monkeypatch, tmp_path):
    before_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "1.0.0")}
    after_pkgs = {"pkg_a": _pkg("https://example.com/a", "tag", "2.0.0")}
    before, after = _commit_two_revs(chdir_repo, before_pkgs, after_pkgs)

    out_file = tmp_path / "matrix.json"
    monkeypatch.setattr(
        sem.sys, "argv",
        ["sweep_eager_matrix.py", "--before", before, "--after", after,
         "--output", str(out_file)],
    )
    sem.main()

    import json

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["include"][0]["package_name"] == "pkg_a"
    assert payload["include"][0]["ref_value"] == "2.0.0"


def test_main_requires_before_and_after(monkeypatch):
    monkeypatch.setattr(sem.sys, "argv", ["sweep_eager_matrix.py", "--before", "x"])
    with pytest.raises(SystemExit):
        sem.main()
