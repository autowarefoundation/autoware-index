"""Tests for scripts/build_envelopes.py — the data-contract core (honest status, H5).

Focus areas (per assignment):
  - status_for(result): the honest-status decision. Never a false green/red.
  - find_result(results_dir, distro, package): matches by CONTENT, not dir name.
  - envelope_for(...): produces a schema-valid record, or None for inconclusive.

The constructed record is validated against schema/history-record.schema.json
using jsonschema, mirroring what main() does before emitting.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

import build_envelopes as be

ZERO_SHA = "0" * 40
GOOD_SHA = "a" * 40


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def record_validator(repo_root):
    """A Draft 2020-12 validator for the history-record schema."""
    schema_path = repo_root / "schema" / "history-record.schema.json"
    schema = json.loads(schema_path.read_text())
    return jsonschema.Draft202012Validator(schema)


def _result(**overrides):
    """A plausible result.json dict; overridable per test."""
    base = {
        "ros_distro": "humble",
        "package_name": "autoware_demo",
        "autoware_version": "1.2.3",
        "build_outcome": "success",
        "test_outcome": "success",
        "resolved_sha": GOOD_SHA,
    }
    base.update(overrides)
    return base


def _row(**overrides):
    base = {
        "ros_distro": "humble",
        "package_name": "autoware_demo",
        "ref_kind": "tag",
        "ref_value": "v1.0.0",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# status_for
# --------------------------------------------------------------------------- #
class TestStatusFor:
    def test_build_and_test_success_is_pass(self):
        assert be.status_for({"build_outcome": "success", "test_outcome": "success"}) == "pass"

    def test_build_failure_is_fail(self):
        assert be.status_for({"build_outcome": "failure", "test_outcome": "success"}) == "fail"

    def test_test_failure_is_fail(self):
        assert be.status_for({"build_outcome": "success", "test_outcome": "failure"}) == "fail"

    def test_both_failure_is_fail(self):
        assert be.status_for({"build_outcome": "failure", "test_outcome": "failure"}) == "fail"

    def test_both_skipped_is_inconclusive(self):
        # Nothing was validated -> must NOT be a false green or red.
        assert be.status_for({"build_outcome": "skipped", "test_outcome": "skipped"}) is None

    def test_build_success_test_skipped_is_inconclusive(self):
        # test never ran, so a clean pass would be a false green.
        assert be.status_for({"build_outcome": "success", "test_outcome": "skipped"}) is None

    def test_build_skipped_test_success_is_inconclusive(self):
        assert be.status_for({"build_outcome": "skipped", "test_outcome": "success"}) is None

    def test_cancelled_is_inconclusive(self):
        assert be.status_for({"build_outcome": "cancelled", "test_outcome": "cancelled"}) is None

    def test_empty_outcomes_is_inconclusive(self):
        assert be.status_for({"build_outcome": "", "test_outcome": ""}) is None

    def test_missing_outcome_keys_is_inconclusive(self):
        assert be.status_for({}) is None

    def test_failure_beats_skipped(self):
        # A genuine failure on one step is `fail` even if the other was skipped.
        assert be.status_for({"build_outcome": "failure", "test_outcome": "skipped"}) == "fail"
        assert be.status_for({"build_outcome": "skipped", "test_outcome": "failure"}) == "fail"

    def test_failure_beats_cancelled(self):
        assert be.status_for({"build_outcome": "cancelled", "test_outcome": "failure"}) == "fail"

    def test_success_plus_failure_is_fail_not_pass(self):
        # One success does not buy a pass when the other genuinely failed.
        assert be.status_for({"build_outcome": "success", "test_outcome": "failure"}) == "fail"


# --------------------------------------------------------------------------- #
# find_result
# --------------------------------------------------------------------------- #
class TestFindResult:
    def _write(self, directory, data):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(json.dumps(data))

    def test_found_in_oddly_named_subdir(self, tmp_path):
        # The directory name does not encode the distro/package; only content does.
        sub = tmp_path / "validate-result-12345"
        self._write(sub, _result(ros_distro="humble", package_name="autoware_demo"))
        found = be.find_result(tmp_path, "humble", "autoware_demo")
        assert found is not None
        assert found["package_name"] == "autoware_demo"
        assert found["ros_distro"] == "humble"

    def test_found_at_root_single_artifact_layout(self, tmp_path):
        # download-artifact extracts straight into the root when there is one match.
        self._write(tmp_path, _result(ros_distro="jazzy", package_name="autoware_planning"))
        found = be.find_result(tmp_path, "jazzy", "autoware_planning")
        assert found is not None
        assert found["package_name"] == "autoware_planning"

    def test_matches_correct_one_among_many(self, tmp_path):
        self._write(tmp_path / "a", _result(ros_distro="humble", package_name="pkg_a"))
        self._write(tmp_path / "b", _result(ros_distro="humble", package_name="pkg_b"))
        self._write(tmp_path / "c", _result(ros_distro="jazzy", package_name="pkg_a"))
        found = be.find_result(tmp_path, "jazzy", "pkg_a")
        assert found is not None
        assert found["ros_distro"] == "jazzy"
        assert found["package_name"] == "pkg_a"

    def test_distro_must_match_too(self, tmp_path):
        # Same package name, different distro -> no match.
        self._write(tmp_path / "x", _result(ros_distro="humble", package_name="pkg"))
        assert be.find_result(tmp_path, "jazzy", "pkg") is None

    def test_package_must_match_too(self, tmp_path):
        self._write(tmp_path / "x", _result(ros_distro="humble", package_name="pkg"))
        assert be.find_result(tmp_path, "humble", "other_pkg") is None

    def test_none_when_dir_empty(self, tmp_path):
        assert be.find_result(tmp_path, "humble", "pkg") is None

    def test_skips_corrupt_json_and_finds_valid(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "result.json").write_text("{not valid json")
        good = tmp_path / "good"
        self._write(good, _result(ros_distro="humble", package_name="pkg"))
        found = be.find_result(tmp_path, "humble", "pkg")
        assert found is not None
        assert found["package_name"] == "pkg"

    def test_corrupt_json_only_returns_none(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "result.json").write_text("totally not json")
        assert be.find_result(tmp_path, "humble", "pkg") is None

    def test_returns_full_dict_contents(self, tmp_path):
        data = _result(ros_distro="humble", package_name="pkg", autoware_version="9.8.7")
        self._write(tmp_path / "deep" / "nested" / "dir", data)
        found = be.find_result(tmp_path, "humble", "pkg")
        assert found["autoware_version"] == "9.8.7"
        assert found["resolved_sha"] == GOOD_SHA


# --------------------------------------------------------------------------- #
# envelope_for
# --------------------------------------------------------------------------- #
class TestEnvelopeFor:
    AT = "2026-05-31T12:00:00Z"
    URL = "https://github.com/autowarefoundation/autoware-index/actions/runs/1"

    def _build(self, row=None, result=None, sweep_kind="eager"):
        return be.envelope_for(
            row or _row(),
            result or _result(),
            sweep_kind,
            self.AT,
            self.URL,
        )

    def test_success_record_is_schema_valid(self, record_validator):
        env, reason = self._build()
        assert reason is None
        assert env is not None
        # The schema validates the record sub-object (as main() does).
        record_validator.validate(env["record"])

    def test_envelope_top_level_shape(self):
        env, _ = self._build(row=_row(ros_distro="jazzy", package_name="autoware_x"))
        assert env["ros_distro"] == "jazzy"
        assert env["package_name"] == "autoware_x"
        assert set(env.keys()) == {"ros_distro", "package_name", "record"}

    def test_record_field_values(self):
        row = _row(ref_kind="branch", ref_value="main")
        result = _result(autoware_version="2.0.0", resolved_sha=GOOD_SHA)
        env, _ = self._build(row=row, result=result, sweep_kind="nightly")
        rec = env["record"]
        assert rec["sweep_kind"] == "nightly"
        assert rec["ref_at_test"] == {"kind": "branch", "value": "main"}
        assert rec["resolved_sha"] == GOOD_SHA
        assert rec["autoware_version"] == "2.0.0"
        assert rec["status"] == "pass"
        assert rec["at"] == self.AT
        assert rec["actions_run_url"] == self.URL

    def test_fail_status_is_schema_valid(self, record_validator):
        result = _result(build_outcome="failure", test_outcome="success")
        env, reason = self._build(result=result)
        assert reason is None
        assert env["record"]["status"] == "fail"
        record_validator.validate(env["record"])

    def test_missing_autoware_version_is_skipped(self):
        result = _result()
        del result["autoware_version"]
        env, reason = self._build(result=result)
        assert env is None
        assert reason is not None
        assert "autoware_version" in reason

    def test_empty_autoware_version_is_skipped(self):
        env, reason = self._build(result=_result(autoware_version=""))
        assert env is None
        assert "autoware_version" in reason

    def test_inconclusive_outcomes_skipped_even_with_version(self):
        # Has a version, but both steps skipped -> still inconclusive.
        result = _result(build_outcome="skipped", test_outcome="skipped")
        env, reason = self._build(result=result)
        assert env is None
        assert reason is not None
        assert "nothing was validated" in reason

    def test_inconclusive_reason_mentions_outcomes(self):
        result = _result(build_outcome="cancelled", test_outcome="")
        env, reason = self._build(result=result)
        assert env is None
        assert "cancelled" in reason

    def test_missing_resolved_sha_falls_back_to_zero_sha(self, record_validator):
        result = _result()
        del result["resolved_sha"]
        env, reason = self._build(result=result)
        assert reason is None
        assert env["record"]["resolved_sha"] == ZERO_SHA
        # ZERO_SHA is 40 hex chars, so still schema-valid.
        record_validator.validate(env["record"])

    def test_wrong_length_resolved_sha_falls_back_to_zero_sha(self, record_validator):
        env, _ = self._build(result=_result(resolved_sha="deadbeef"))
        assert env["record"]["resolved_sha"] == ZERO_SHA
        record_validator.validate(env["record"])

    def test_empty_resolved_sha_falls_back_to_zero_sha(self):
        env, _ = self._build(result=_result(resolved_sha=""))
        assert env["record"]["resolved_sha"] == ZERO_SHA

    def test_correct_length_sha_is_kept(self):
        sha = "b" * 40
        env, _ = self._build(result=_result(resolved_sha=sha))
        assert env["record"]["resolved_sha"] == sha

    def test_missing_row_key_raises(self):
        # envelope_for indexes row[...] directly; a malformed row is a hard error.
        bad_row = {"ros_distro": "humble"}  # missing package_name etc.
        with pytest.raises(KeyError):
            self._build(row=bad_row)

    def test_record_for_each_sweep_kind_validates(self, record_validator):
        for kind in ("eager", "nightly"):
            env, reason = self._build(sweep_kind=kind)
            assert reason is None
            record_validator.validate(env["record"])

    def test_each_ref_kind_validates(self, record_validator):
        for kind in ("tag", "sha", "branch"):
            env, _ = self._build(row=_row(ref_kind=kind, ref_value="x"))
            record_validator.validate(env["record"])


# --------------------------------------------------------------------------- #
# module-level constants
# --------------------------------------------------------------------------- #
class TestConstants:
    def test_zero_sha_is_40_hex_zeros(self):
        assert be.ZERO_SHA == "0" * 40
        assert len(be.ZERO_SHA) == 40

    def test_schema_path_points_at_history_record_schema(self):
        assert be.SCHEMA_PATH.name == "history-record.schema.json"
        assert be.SCHEMA_PATH.is_file()
