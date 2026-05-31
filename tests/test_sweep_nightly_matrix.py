"""Tests for scripts/sweep_nightly_matrix.py.

build_matrix(distributions_dir) emits one include row for EVERY `kind: branch`
package across all distributions/*.yaml and EXCLUDES `tag`/`sha` pins. These
tests build temp distribution dirs with a mix of branch/tag/sha refs across
multiple distros and assert only branch entries appear with the correct fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import sweep_nightly_matrix


def _write_distro(directory: Path, filename: str, doc: dict) -> Path:
    """Write a distribution YAML doc and return its path."""
    path = directory / filename
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Core behavior: branch-only inclusion, tag/sha exclusion, field shape.
# --------------------------------------------------------------------------- #


def test_branch_only_across_two_distros(tmp_path):
    """A mix of branch/tag/sha across two distros yields only the branch rows."""
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "pkg_branch": {
                    "repository": "https://example.com/pkg_branch",
                    "ref": {"kind": "branch", "value": "main"},
                },
                "pkg_tag": {
                    "repository": "https://example.com/pkg_tag",
                    "ref": {"kind": "tag", "value": "v1.0.0"},
                },
                "pkg_sha": {
                    "repository": "https://example.com/pkg_sha",
                    "ref": {"kind": "sha", "value": "deadbeef"},
                },
            },
        },
    )
    _write_distro(
        tmp_path,
        "humble.yaml",
        {
            "ros_distro": "humble",
            "packages": {
                "pkg_branch_h": {
                    "repository": "https://example.com/pkg_branch_h",
                    "ref": {"kind": "branch", "value": "develop"},
                },
                "pkg_tag_h": {
                    "repository": "https://example.com/pkg_tag_h",
                    "ref": {"kind": "tag", "value": "v2.0.0"},
                },
            },
        },
    )

    rows = sweep_nightly_matrix.build_matrix(tmp_path)

    names = {r["package_name"] for r in rows}
    assert names == {"pkg_branch", "pkg_branch_h"}
    # Every row is a branch ref.
    assert all(r["ref_kind"] == "branch" for r in rows)


def test_row_field_shape_and_values(tmp_path):
    """Each branch row carries exactly the five documented fields with values."""
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "autoware_livox_tag_filter": {
                    "repository": "https://github.com/autowarefoundation/autoware_livox_tag_filter",
                    "ref": {"kind": "branch", "value": "main"},
                },
            },
        },
    )

    rows = sweep_nightly_matrix.build_matrix(tmp_path)

    assert rows == [
        {
            "ros_distro": "jazzy",
            "package_name": "autoware_livox_tag_filter",
            "package_repository": "https://github.com/autowarefoundation/autoware_livox_tag_filter",
            "ref_kind": "branch",
            "ref_value": "main",
        }
    ]
    # Exactly the documented keys, nothing extra.
    assert set(rows[0]) == {
        "ros_distro",
        "package_name",
        "package_repository",
        "ref_kind",
        "ref_value",
    }


def test_tag_and_sha_only_distro_yields_no_rows(tmp_path):
    """A distro composed solely of tag/sha pins contributes nothing."""
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "pkg_tag": {
                    "repository": "https://example.com/pkg_tag",
                    "ref": {"kind": "tag", "value": "v1.2.3"},
                },
                "pkg_sha": {
                    "repository": "https://example.com/pkg_sha",
                    "ref": {"kind": "sha", "value": "abc123def456"},
                },
            },
        },
    )

    assert sweep_nightly_matrix.build_matrix(tmp_path) == []


# --------------------------------------------------------------------------- #
# Ordering: glob is sorted by filename; package order preserved within a file.
# --------------------------------------------------------------------------- #


def test_distros_processed_in_sorted_filename_order(tmp_path):
    """Rows appear grouped by the sorted *.yaml filename order."""
    # 'aaa.yaml' must come before 'zzz.yaml' regardless of write order.
    _write_distro(
        tmp_path,
        "zzz.yaml",
        {
            "ros_distro": "zdistro",
            "packages": {
                "z_pkg": {
                    "repository": "https://example.com/z_pkg",
                    "ref": {"kind": "branch", "value": "main"},
                },
            },
        },
    )
    _write_distro(
        tmp_path,
        "aaa.yaml",
        {
            "ros_distro": "adistro",
            "packages": {
                "a_pkg": {
                    "repository": "https://example.com/a_pkg",
                    "ref": {"kind": "branch", "value": "main"},
                },
            },
        },
    )

    rows = sweep_nightly_matrix.build_matrix(tmp_path)

    assert [r["package_name"] for r in rows] == ["a_pkg", "z_pkg"]


def test_package_order_within_file_preserved(tmp_path):
    """Within one distro, branch rows keep their YAML declaration order."""
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "pkg_one": {
                    "repository": "https://example.com/pkg_one",
                    "ref": {"kind": "branch", "value": "main"},
                },
                "pkg_two": {
                    "repository": "https://example.com/pkg_two",
                    "ref": {"kind": "branch", "value": "feature"},
                },
                "pkg_three": {
                    "repository": "https://example.com/pkg_three",
                    "ref": {"kind": "branch", "value": "dev"},
                },
            },
        },
    )

    rows = sweep_nightly_matrix.build_matrix(tmp_path)

    assert [r["package_name"] for r in rows] == ["pkg_one", "pkg_two", "pkg_three"]


# --------------------------------------------------------------------------- #
# Edge cases: missing/empty fields, empty docs, non-yaml files.
# --------------------------------------------------------------------------- #


def test_branch_ref_missing_repository_is_skipped(tmp_path, capsys):
    """A branch ref with no repository is skipped with a stderr notice."""
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "no_repo": {
                    "ref": {"kind": "branch", "value": "main"},
                },
            },
        },
    )

    rows = sweep_nightly_matrix.build_matrix(tmp_path)

    assert rows == []
    err = capsys.readouterr().err
    assert "skipping" in err
    assert "no_repo" in err


def test_branch_ref_missing_value_is_skipped(tmp_path, capsys):
    """A branch ref with no value is skipped (cannot resolve a tip)."""
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "no_value": {
                    "repository": "https://example.com/no_value",
                    "ref": {"kind": "branch"},
                },
            },
        },
    )

    rows = sweep_nightly_matrix.build_matrix(tmp_path)

    assert rows == []
    assert "no_value" in capsys.readouterr().err


def test_distro_missing_ros_distro_skips_its_branch_packages(tmp_path, capsys):
    """Without ros_distro, even branch packages are skipped."""
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {
            # no ros_distro key
            "packages": {
                "orphan": {
                    "repository": "https://example.com/orphan",
                    "ref": {"kind": "branch", "value": "main"},
                },
            },
        },
    )

    rows = sweep_nightly_matrix.build_matrix(tmp_path)

    assert rows == []
    assert "orphan" in capsys.readouterr().err


def test_empty_yaml_file_is_tolerated(tmp_path):
    """An empty doc (safe_load -> None) is handled via the `or {}` guard."""
    (tmp_path / "empty.yaml").write_text("", encoding="utf-8")

    assert sweep_nightly_matrix.build_matrix(tmp_path) == []


def test_doc_without_packages_key_is_tolerated(tmp_path):
    """A doc with ros_distro but no packages mapping yields no rows."""
    _write_distro(tmp_path, "jazzy.yaml", {"ros_distro": "jazzy"})

    assert sweep_nightly_matrix.build_matrix(tmp_path) == []


def test_packages_value_explicit_null_is_tolerated(tmp_path):
    """packages: null collapses to {} via the `or {}` guard."""
    _write_distro(tmp_path, "jazzy.yaml", {"ros_distro": "jazzy", "packages": None})

    assert sweep_nightly_matrix.build_matrix(tmp_path) == []


def test_package_spec_null_is_tolerated(tmp_path):
    """A package whose spec is null has no ref/branch and is excluded."""
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {"ros_distro": "jazzy", "packages": {"nullspec": None}},
    )

    assert sweep_nightly_matrix.build_matrix(tmp_path) == []


def test_package_without_ref_block_is_excluded(tmp_path):
    """A package with no ref block is not a branch ref, so it is excluded."""
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "no_ref": {"repository": "https://example.com/no_ref"},
            },
        },
    )

    assert sweep_nightly_matrix.build_matrix(tmp_path) == []


def test_non_yaml_files_ignored(tmp_path):
    """Only *.yaml is globbed; *.yml and *.json siblings are ignored."""
    # A .yml file that, if globbed, would add a row.
    (tmp_path / "extra.yml").write_text(
        yaml.safe_dump(
            {
                "ros_distro": "humble",
                "packages": {
                    "yml_pkg": {
                        "repository": "https://example.com/yml_pkg",
                        "ref": {"kind": "branch", "value": "main"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "yaml_pkg": {
                    "repository": "https://example.com/yaml_pkg",
                    "ref": {"kind": "branch", "value": "main"},
                }
            },
        },
    )

    rows = sweep_nightly_matrix.build_matrix(tmp_path)

    assert [r["package_name"] for r in rows] == ["yaml_pkg"]


def test_empty_distributions_dir_yields_no_rows(tmp_path):
    """An empty directory produces an empty matrix, not an error."""
    assert sweep_nightly_matrix.build_matrix(tmp_path) == []


def test_unknown_ref_kind_is_excluded(tmp_path):
    """A ref kind other than branch/tag/sha is still excluded (branch-only)."""
    _write_distro(
        tmp_path,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "weird": {
                    "repository": "https://example.com/weird",
                    "ref": {"kind": "release", "value": "x"},
                }
            },
        },
    )

    assert sweep_nightly_matrix.build_matrix(tmp_path) == []


# --------------------------------------------------------------------------- #
# main(): end-to-end argument handling and JSON envelope.
# --------------------------------------------------------------------------- #


def test_main_writes_compact_include_envelope_to_file(tmp_path, monkeypatch, capsys):
    """main(--output file) writes a compact {"include": [...]} JSON payload."""
    dist = tmp_path / "distributions"
    dist.mkdir()
    _write_distro(
        dist,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "pkg_branch": {
                    "repository": "https://example.com/pkg_branch",
                    "ref": {"kind": "branch", "value": "main"},
                },
                "pkg_tag": {
                    "repository": "https://example.com/pkg_tag",
                    "ref": {"kind": "tag", "value": "v1"},
                },
            },
        },
    )
    out = tmp_path / "matrix.json"

    monkeypatch.setattr(
        sweep_nightly_matrix.sys,
        "argv",
        [
            "sweep_nightly_matrix.py",
            "--distributions-dir",
            str(dist),
            "--output",
            str(out),
        ],
    )
    sweep_nightly_matrix.main()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload) == {"include"}
    assert [r["package_name"] for r in payload["include"]] == ["pkg_branch"]
    # Compact separators: no spaces after ',' or ':'.
    raw = out.read_text(encoding="utf-8")
    assert ", " not in raw
    assert '": ' not in raw


def test_main_writes_envelope_to_stdout(tmp_path, monkeypatch, capsys):
    """main(--output -) emits the JSON envelope on stdout."""
    dist = tmp_path / "distributions"
    dist.mkdir()
    _write_distro(
        dist,
        "jazzy.yaml",
        {
            "ros_distro": "jazzy",
            "packages": {
                "pkg_branch": {
                    "repository": "https://example.com/pkg_branch",
                    "ref": {"kind": "branch", "value": "main"},
                },
            },
        },
    )

    monkeypatch.setattr(
        sweep_nightly_matrix.sys,
        "argv",
        [
            "sweep_nightly_matrix.py",
            "--distributions-dir",
            str(dist),
        ],
    )
    sweep_nightly_matrix.main()

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert [r["package_name"] for r in payload["include"]] == ["pkg_branch"]


def test_main_empty_dir_emits_empty_include(tmp_path, monkeypatch, capsys):
    """With no distributions, main() still emits a valid empty envelope."""
    dist = tmp_path / "distributions"
    dist.mkdir()

    monkeypatch.setattr(
        sweep_nightly_matrix.sys,
        "argv",
        ["sweep_nightly_matrix.py", "--distributions-dir", str(dist)],
    )
    sweep_nightly_matrix.main()

    assert json.loads(capsys.readouterr().out) == {"include": []}


# --------------------------------------------------------------------------- #
# Sanity: the real repo distributions parse and only yield branch rows.
# --------------------------------------------------------------------------- #


def test_real_repo_distributions_yield_only_branch_rows(repo_root):
    """Smoke test against the committed distributions/ dir."""
    dist = repo_root / "distributions"
    if not dist.is_dir():
        pytest.skip("no distributions/ dir in repo")

    rows = sweep_nightly_matrix.build_matrix(dist)
    # Whatever is registered, every emitted row must be a branch ref with all
    # five fields populated.
    for r in rows:
        assert r["ref_kind"] == "branch"
        assert r["ros_distro"]
        assert r["package_repository"]
        assert r["ref_value"]
        assert set(r) == {
            "ros_distro",
            "package_name",
            "package_repository",
            "ref_kind",
            "ref_value",
        }
