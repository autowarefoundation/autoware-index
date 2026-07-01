"""Tests for scripts/build_envelopes.py (repo artifact -> per-package envelopes).

The recorder is the honesty chokepoint (locked decision 6): per package,
`pass` only on success/success of ITS OWN closure build + ITS OWN tests,
`fail` only on a real failure, anything else (absent from tree, null
outcomes, missing artifact) is a loud skip — never a fabricated record.
It also derives the state-advance set (a row advances only when EVERY
registered package recorded conclusively) and stages the artifact-shipped
package.xml files, and it hard-fails when a non-empty matrix records nothing.
"""

import json

import build_envelopes as m
import pytest


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def make_row(packages="autoware_a_filter zz_planner_b", **over):
    row = {
        "ros_distro": "jazzy",
        "repo_name": "awesome_tools",
        "repository": "https://github.com/example-org/awesome_tools",
        "ref_kind": "tag",
        "ref_value": "1.2.0",
        "packages": packages,
    }
    row.update(over)
    return row


def make_result(packages=None, **over):
    result = {
        "schema": 2,
        "ros_distro": "jazzy",
        "autoware_version": "1.8.0",
        "repo_name": "awesome_tools",
        "repository": "https://github.com/example-org/awesome_tools",
        "ref": {"kind": "tag", "value": "1.2.0"},
        "resolved_sha": "a" * 40,
        "packages": (
            packages
            if packages is not None
            else {
                "autoware_a_filter": {
                    "present": True,
                    "build_outcome": "success",
                    "test_outcome": "success",
                },
                "zz_planner_b": {
                    "present": True,
                    "build_outcome": "success",
                    "test_outcome": "success",
                },
            }
        ),
    }
    result.update(over)
    return result


def write_artifact(
    results_dir, result, xmls=None, subdir="validate-result-jazzy-awesome_tools-1.8.0"
):
    """Lay out one downloaded artifact: result.json + package-xmls/<pkg>.xml."""
    art = results_dir / subdir
    art.mkdir(parents=True, exist_ok=True)
    (art / "result.json").write_text(json.dumps(result))
    for pkg, content in (xmls or {}).items():
        xml_dir = art / "package-xmls"
        xml_dir.mkdir(exist_ok=True)
        (xml_dir / f"{pkg}.xml").write_text(content)
    return art


# --------------------------------------------------------------------------
# status_for — the per-package honesty mapping
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ({"present": True, "build_outcome": "success", "test_outcome": "success"}, "pass"),
        ({"present": True, "build_outcome": "failure", "test_outcome": None}, "fail"),
        ({"present": True, "build_outcome": "success", "test_outcome": "failure"}, "fail"),
        ({"present": True, "build_outcome": "failure", "test_outcome": "failure"}, "fail"),
        # Inconclusive shapes: absent, half-run, cancelled. Never pass/fail.
        ({"present": False, "build_outcome": None, "test_outcome": None}, None),
        ({"present": False, "build_outcome": "success", "test_outcome": "success"}, None),
        ({"present": True, "build_outcome": None, "test_outcome": None}, None),
        ({"present": True, "build_outcome": "success", "test_outcome": None}, None),
        ({"present": True, "build_outcome": "cancelled", "test_outcome": ""}, None),
        ({}, None),
    ],
)
def test_status_for(outcome, expected):
    assert m.status_for(outcome) == expected


# --------------------------------------------------------------------------
# find_result — content-keyed artifact discovery (layout-independent)
# --------------------------------------------------------------------------
def test_find_result_matches_on_content_not_dirname(tmp_path):
    write_artifact(tmp_path, make_result(), subdir="weirdly-named-dir")
    found = m.find_result(tmp_path, "jazzy", "awesome_tools")
    assert found is not None
    assert found.name == "result.json"
    assert found.parent.name == "weirdly-named-dir"


def test_find_result_root_extraction_layout(tmp_path):
    # Single-artifact downloads extract to the path root, no subdir.
    (tmp_path / "result.json").write_text(json.dumps(make_result()))
    assert m.find_result(tmp_path, "jazzy", "awesome_tools") == tmp_path / "result.json"


def test_find_result_distinguishes_repos_and_distros(tmp_path):
    write_artifact(tmp_path, make_result(), subdir="a")
    write_artifact(tmp_path, make_result(repo_name="other_repo"), subdir="b")
    write_artifact(tmp_path, make_result(ros_distro="humble"), subdir="c")
    assert m.find_result(tmp_path, "jazzy", "other_repo").parent.name == "b"
    assert m.find_result(tmp_path, "humble", "awesome_tools").parent.name == "c"


def test_find_result_ignores_v1_results_and_garbage(tmp_path):
    # A legacy per-package result.json has no "schema": 2 — never matched.
    legacy = {"ros_distro": "jazzy", "package_name": "awesome_tools", "build_outcome": "success"}
    write_artifact(tmp_path, legacy, subdir="legacy")
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "result.json").write_text("{not json")
    assert m.find_result(tmp_path, "jazzy", "awesome_tools") is None


def test_find_result_empty_dir(tmp_path):
    assert m.find_result(tmp_path, "jazzy", "awesome_tools") is None


# --------------------------------------------------------------------------
# envelopes_for_row — fan-out + per-package skips
# --------------------------------------------------------------------------
AT = "2026-06-11T12:00:00Z"
URL = "https://example.com/run/42"


def test_envelopes_for_row_happy_path_two_packages():
    envelopes, skips = m.envelopes_for_row(make_row(), make_result(), "eager", AT, URL)
    assert skips == []
    assert [e["package_name"] for e in envelopes] == ["autoware_a_filter", "zz_planner_b"]
    for e in envelopes:
        r = e["record"]
        assert r["schema"] == 2
        assert r["sweep_kind"] == "eager"
        assert r["ref_at_test"] == {"kind": "tag", "value": "1.2.0"}
        assert r["resolved_sha"] == "a" * 40
        assert r["autoware_version"] == "1.8.0"
        assert r["status"] == "pass"
        assert r["at"] == AT
        assert r["actions_run_url"] == URL
        assert r["repository"] == "https://github.com/example-org/awesome_tools"
        assert r["repo_name"] == "awesome_tools"


def test_envelopes_for_row_sibling_statuses_independent():
    result = make_result(
        packages={
            "autoware_a_filter": {
                "present": True,
                "build_outcome": "success",
                "test_outcome": "success",
            },
            "zz_planner_b": {
                "present": True,
                "build_outcome": "success",
                "test_outcome": "failure",
            },
        }
    )
    envelopes, skips = m.envelopes_for_row(make_row(), result, "nightly", AT, URL)
    by_name = {e["package_name"]: e["record"]["status"] for e in envelopes}
    assert by_name == {"autoware_a_filter": "pass", "zz_planner_b": "fail"}
    assert skips == []


def test_envelopes_for_row_absent_package_skipped_loudly():
    result = make_result(
        packages={
            "autoware_a_filter": {
                "present": True,
                "build_outcome": "success",
                "test_outcome": "success",
            },
            "zz_planner_b": {"present": False, "build_outcome": None, "test_outcome": None},
        }
    )
    envelopes, skips = m.envelopes_for_row(make_row(), result, "eager", AT, URL)
    assert [e["package_name"] for e in envelopes] == ["autoware_a_filter"]
    assert len(skips) == 1 and "zz_planner_b" in skips[0] and "inconclusive" in skips[0]


def test_envelopes_for_row_package_missing_from_result():
    result = make_result(
        packages={
            "autoware_a_filter": {
                "present": True,
                "build_outcome": "success",
                "test_outcome": "success",
            }
        }
    )
    envelopes, skips = m.envelopes_for_row(make_row(), result, "eager", AT, URL)
    assert len(envelopes) == 1
    assert len(skips) == 1 and "no outcome" in skips[0]


def test_envelopes_for_row_no_autoware_version_skips_whole_row():
    envelopes, skips = m.envelopes_for_row(
        make_row(), make_result(autoware_version=""), "eager", AT, URL
    )
    assert envelopes == []
    assert len(skips) == 1 and "autoware_version" in skips[0]


@pytest.mark.parametrize("bad_sha", ["short", "", None, "Z" * 40, "0" * 39])
def test_envelopes_for_row_invalid_sha_skips_whole_row(bad_sha):
    # No real sha = clone/resolve never completed = nothing validated.
    # Fabricating a sentinel sha inside conclusive records would be false
    # provenance; the whole row is an inconclusive loud skip.
    envelopes, skips = m.envelopes_for_row(
        make_row(), make_result(resolved_sha=bad_sha), "eager", AT, URL
    )
    assert envelopes == []
    assert len(skips) == 1 and "resolved_sha" in skips[0]


# --------------------------------------------------------------------------
# stage_metadata — artifact-shipped package.xml -> staged metadata tree
# --------------------------------------------------------------------------
def test_stage_metadata_copies_present_files(tmp_path):
    art = write_artifact(
        tmp_path / "results",
        make_result(),
        xmls={"autoware_a_filter": "<package><name>autoware_a_filter</name></package>"},
    )
    out = tmp_path / "staged"
    staged = m.stage_metadata(art / "result.json", make_row(), out)
    assert staged == 1
    assert (out / "jazzy" / "autoware_a_filter.xml").read_text().startswith("<package>")
    # zz_planner_b had no shipped xml: not staged, no crash, no empty file.
    assert not (out / "jazzy" / "zz_planner_b.xml").exists()


def test_stage_metadata_no_xml_dir(tmp_path):
    art = write_artifact(tmp_path / "results", make_result())
    assert m.stage_metadata(art / "result.json", make_row(), tmp_path / "staged") == 0


# --------------------------------------------------------------------------
# main() — end to end on tmp dirs
# --------------------------------------------------------------------------
def run_main(monkeypatch, tmp_path, rows, *, sweep_kind="eager"):
    matrix_file = tmp_path / "matrix.json"
    matrix_file.write_text(json.dumps({"include": rows}))
    out = tmp_path / "envelopes.json"
    states = tmp_path / "states.json"
    metadata = tmp_path / "staged-metadata"
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_envelopes.py",
            "--matrix-file",
            str(matrix_file),
            "--results-dir",
            str(tmp_path / "results"),
            "--sweep-kind",
            sweep_kind,
            "--actions-run-url",
            URL,
            "--output",
            str(out),
            "--states-output",
            str(states),
            "--metadata-output",
            str(metadata),
        ],
    )
    m.main()
    return json.loads(out.read_text()), json.loads(states.read_text()), metadata


def test_main_full_row_advances_state(monkeypatch, tmp_path):
    write_artifact(
        tmp_path / "results",
        make_result(),
        xmls={
            "autoware_a_filter": "<package><name>autoware_a_filter</name></package>",
            "zz_planner_b": "<package><name>zz_planner_b</name></package>",
        },
    )
    envelopes, states, metadata = run_main(monkeypatch, tmp_path, [make_row()])

    assert len(envelopes) == 2
    assert len(states) == 1
    state = states[0]
    assert state["ros_distro"] == "jazzy" and state["repo_name"] == "awesome_tools"
    assert state["state"]["url"] == "https://github.com/example-org/awesome_tools"
    assert state["state"]["ref"] == {"kind": "tag", "value": "1.2.0"}
    assert state["state"]["packages"] == ["autoware_a_filter", "zz_planner_b"]
    assert state["state"]["last_run_url"] == URL
    assert (metadata / "jazzy" / "autoware_a_filter.xml").exists()
    assert (metadata / "jazzy" / "zz_planner_b.xml").exists()


def test_main_partial_row_does_not_advance_state(monkeypatch, tmp_path, capsys):
    # One package absent: its envelope is skipped, so the row must NOT
    # advance — the level-triggered discover re-sweeps it until conclusive.
    write_artifact(
        tmp_path / "results",
        make_result(
            packages={
                "autoware_a_filter": {
                    "present": True,
                    "build_outcome": "success",
                    "test_outcome": "success",
                },
                "zz_planner_b": {"present": False, "build_outcome": None, "test_outcome": None},
            }
        ),
    )
    envelopes, states, _ = run_main(monkeypatch, tmp_path, [make_row()])
    assert len(envelopes) == 1
    assert states == []
    err = capsys.readouterr().err
    assert "state not advanced" in err


def test_main_fail_records_still_advance_state(monkeypatch, tmp_path):
    # fail IS conclusive — a red row records and advances; only inconclusive
    # rows re-sweep. (Otherwise a genuinely broken repo would re-sweep nightly
    # forever for no new information.)
    write_artifact(
        tmp_path / "results",
        make_result(
            packages={
                "autoware_a_filter": {
                    "present": True,
                    "build_outcome": "failure",
                    "test_outcome": None,
                },
                "zz_planner_b": {
                    "present": True,
                    "build_outcome": "success",
                    "test_outcome": "success",
                },
            }
        ),
    )
    envelopes, states, _ = run_main(monkeypatch, tmp_path, [make_row()])
    assert {e["record"]["status"] for e in envelopes} == {"fail", "pass"}
    assert len(states) == 1


def test_main_missing_artifact_skips_row_loudly(monkeypatch, tmp_path, capsys):
    write_artifact(tmp_path / "results", make_result())  # only awesome_tools
    other = make_row(
        repo_name="other_repo", repository="https://github.com/x/other_repo", packages="p"
    )
    envelopes, states, _ = run_main(monkeypatch, tmp_path, [make_row(), other])
    assert len(envelopes) == 2  # awesome_tools' two packages
    assert len(states) == 1
    assert "no result artifact for jazzy/other_repo" in capsys.readouterr().err


def test_main_zero_envelopes_from_nonempty_matrix_hard_fails(monkeypatch, tmp_path):
    # No artifacts at all: infra fault, not package failure -> exit non-zero
    # (locked decision 5 applies to package outcomes, not pipeline faults).
    (tmp_path / "results").mkdir()
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, tmp_path, [make_row()])
    assert "ZERO envelopes" in str(exc.value)


def test_main_empty_matrix_is_fine(monkeypatch, tmp_path):
    (tmp_path / "results").mkdir()
    envelopes, states, _ = run_main(monkeypatch, tmp_path, [])
    assert envelopes == [] and states == []


def test_main_schema_invalid_record_dropped(monkeypatch, tmp_path, capsys):
    # A non-semver autoware_version passes envelope construction but fails
    # history-record schema validation: dropped loudly, and with every
    # envelope dropped the zero-envelope tripwire fires.
    write_artifact(tmp_path / "results", make_result(autoware_version="garbage"))
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tmp_path, [make_row()])
    assert "fails schema" in capsys.readouterr().err


def test_main_emitted_records_validate_against_schema(monkeypatch, tmp_path):
    import jsonschema

    write_artifact(tmp_path / "results", make_result())
    envelopes, _, _ = run_main(monkeypatch, tmp_path, [make_row()], sweep_kind="nightly")
    schema = json.loads(m.SCHEMA_PATH.read_text())
    for e in envelopes:
        jsonschema.Draft202012Validator(schema).validate(e["record"])
