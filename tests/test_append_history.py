"""Tests for scripts/append_history.py.

Covers the pure parts (load_envelopes, build_commit_message) against real temp
files / stdin, and exercises append_history's file-routing + control flow by
monkeypatching the git/push side effects (no real git, no real pushes).

NOTE: the module/CLI docstrings say the input is "NDJSON", but load_envelopes
actually uses json.load (a single JSON array). Tests assert the REAL behavior.
"""

from __future__ import annotations

import io
import json
import subprocess
import types

import pytest

import append_history as ah


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_record(
    sweep_kind="build",
    status="pass",
    autoware_version="1.0.0",
):
    return {
        "sweep_kind": sweep_kind,
        "status": status,
        "autoware_version": autoware_version,
    }


def make_envelope(
    ros_distro="jazzy",
    package_name="autoware_demo",
    record=None,
):
    return {
        "ros_distro": ros_distro,
        "package_name": package_name,
        "record": record if record is not None else make_record(),
    }


# ---------------------------------------------------------------------------
# load_envelopes
# ---------------------------------------------------------------------------

def test_load_envelopes_from_file(tmp_path):
    envelopes = [make_envelope(package_name="pkg_a"), make_envelope(package_name="pkg_b")]
    src = tmp_path / "records.json"
    src.write_text(json.dumps(envelopes), encoding="utf-8")

    result = ah.load_envelopes(str(src))

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["package_name"] == "pkg_a"
    assert result[1]["package_name"] == "pkg_b"
    assert result[0]["record"]["sweep_kind"] == "build"


def test_load_envelopes_empty_array_from_file(tmp_path):
    src = tmp_path / "empty.json"
    src.write_text("[]", encoding="utf-8")

    assert ah.load_envelopes(str(src)) == []


def test_load_envelopes_from_stdin(monkeypatch):
    envelopes = [make_envelope(ros_distro="humble", package_name="pkg_stdin")]
    monkeypatch.setattr(ah.sys, "stdin", io.StringIO(json.dumps(envelopes)))

    result = ah.load_envelopes("-")

    assert isinstance(result, list)
    assert result == envelopes


def test_load_envelopes_non_list_exits(tmp_path):
    # A JSON object (not an array) must be rejected via sys.exit.
    src = tmp_path / "obj.json"
    src.write_text(json.dumps({"ros_distro": "jazzy"}), encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        ah.load_envelopes(str(src))
    # sys.exit("msg") carries the message as the exit code value.
    assert "must be an array" in str(excinfo.value)


def test_load_envelopes_non_list_from_stdin_exits(monkeypatch):
    monkeypatch.setattr(ah.sys, "stdin", io.StringIO('"just a string"'))
    with pytest.raises(SystemExit):
        ah.load_envelopes("-")


def test_load_envelopes_missing_file_raises(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(FileNotFoundError):
        ah.load_envelopes(str(missing))


def test_load_envelopes_invalid_json_raises(tmp_path):
    src = tmp_path / "bad.json"
    src.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        ah.load_envelopes(str(src))


# ---------------------------------------------------------------------------
# build_commit_message
# ---------------------------------------------------------------------------

def test_build_commit_message_single():
    env = make_envelope(
        ros_distro="jazzy",
        package_name="autoware_livox_tag_filter",
        record=make_record(sweep_kind="release", status="fail", autoware_version="2.3.4"),
    )
    msg = ah.build_commit_message([env])
    assert msg == (
        "chore(data): release sweep fail "
        "for autoware_livox_tag_filter @ jazzy / autoware 2.3.4"
    )


def test_build_commit_message_multiple_summarizes_and_sorts():
    envelopes = [
        make_envelope(ros_distro="rolling", record=make_record(sweep_kind="release")),
        make_envelope(ros_distro="humble", record=make_record(sweep_kind="build")),
        make_envelope(ros_distro="humble", record=make_record(sweep_kind="build")),
    ]
    msg = ah.build_commit_message(envelopes)
    # count is total envelopes (3), kinds deduped+sorted, distros deduped+sorted.
    assert msg == (
        "chore(data): append 3 sweep result(s) "
        "[build,release] across humble,rolling"
    )


def test_build_commit_message_multiple_dedupes_kinds_and_distros():
    envelopes = [
        make_envelope(ros_distro="jazzy", record=make_record(sweep_kind="build")),
        make_envelope(ros_distro="jazzy", record=make_record(sweep_kind="build")),
    ]
    msg = ah.build_commit_message(envelopes)
    assert msg == (
        "chore(data): append 2 sweep result(s) [build] across jazzy"
    )


# ---------------------------------------------------------------------------
# append_history -- side effects monkeypatched
# ---------------------------------------------------------------------------

def test_append_history_empty_is_noop(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ah, "run", lambda *a, **k: calls.append(a))

    ah.append_history([])

    assert calls == []  # never touched git
    assert "no records to append" in capsys.readouterr().err


class _FakeRun:
    """Records git commands routed through the module-level run() helper.

    Returns a CompletedProcess whose stdout is controllable per-command so the
    `git status --porcelain` gate reports pending changes.
    """

    def __init__(self, status_stdout="?? history/jazzy/autoware_demo.ndjson"):
        self.calls = []
        self.status_stdout = status_stdout

    def __call__(self, cmd, cwd, check=True):
        self.calls.append(list(cmd))
        stdout = ""
        if cmd[:3] == ["git", "status", "--porcelain"]:
            stdout = self.status_stdout
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


def _patch_worktree_tmpdir(monkeypatch, tmp_path):
    """Force append_history to use a known worktree dir under tmp_path."""
    worktree = tmp_path / "data-worktree"
    worktree.mkdir()
    monkeypatch.setattr(ah.tempfile, "mkdtemp", lambda prefix="": str(worktree))
    return worktree


def test_append_history_writes_one_ndjson_line_per_envelope(monkeypatch, tmp_path):
    worktree = _patch_worktree_tmpdir(monkeypatch, tmp_path)
    fake_run = _FakeRun()
    monkeypatch.setattr(ah, "run", fake_run)

    pushes = []

    def fake_push(cmd, cwd, capture_output, text):
        pushes.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ah.subprocess, "run", fake_push)

    envelopes = [
        make_envelope(ros_distro="jazzy", package_name="pkg_a", record=make_record(status="pass")),
        make_envelope(ros_distro="humble", package_name="pkg_b", record=make_record(status="fail")),
    ]
    ah.append_history(envelopes)

    f_a = worktree / "history" / "jazzy" / "pkg_a.ndjson"
    f_b = worktree / "history" / "humble" / "pkg_b.ndjson"
    assert f_a.exists() and f_b.exists()

    lines_a = f_a.read_text(encoding="utf-8").splitlines()
    lines_b = f_b.read_text(encoding="utf-8").splitlines()
    assert len(lines_a) == 1
    assert len(lines_b) == 1
    # compact json, one record per line, round-trippable
    assert json.loads(lines_a[0]) == make_record(status="pass")
    assert json.loads(lines_b[0]) == make_record(status="fail")
    # compact separators -> no spaces after ':' or ','
    assert ", " not in lines_a[0]
    assert lines_a[0].endswith("}")

    # exactly one push happened, to HEAD:data
    assert len(pushes) == 1
    assert pushes[0] == ["git", "push", "origin", "HEAD:data"]


def test_append_history_appends_multiple_lines_to_same_file(monkeypatch, tmp_path):
    worktree = _patch_worktree_tmpdir(monkeypatch, tmp_path)
    monkeypatch.setattr(ah, "run", _FakeRun())
    monkeypatch.setattr(
        ah.subprocess,
        "run",
        lambda cmd, cwd, capture_output, text: subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    # pre-seed an existing line; appended write must not truncate it
    target = worktree / "history" / "jazzy" / "pkg_a.ndjson"
    target.parent.mkdir(parents=True)
    target.write_text('{"pre":"existing"}\n', encoding="utf-8")

    envelopes = [
        make_envelope(ros_distro="jazzy", package_name="pkg_a", record=make_record(status="r1")),
        make_envelope(ros_distro="jazzy", package_name="pkg_a", record=make_record(status="r2")),
    ]
    ah.append_history(envelopes)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"pre":"existing"}'
    assert json.loads(lines[1])["status"] == "r1"
    assert json.loads(lines[2])["status"] == "r2"
    assert len(lines) == 3


def test_append_history_no_changes_returns_without_push(monkeypatch, tmp_path):
    _patch_worktree_tmpdir(monkeypatch, tmp_path)
    # status reports empty -> "no changes to commit" early return, no commit/push.
    monkeypatch.setattr(ah, "run", _FakeRun(status_stdout=""))

    pushes = []
    monkeypatch.setattr(
        ah.subprocess,
        "run",
        lambda *a, **k: pushes.append(a) or subprocess.CompletedProcess([], 0, "", ""),
    )

    ah.append_history([make_envelope()])
    assert pushes == []


def test_append_history_retries_then_succeeds(monkeypatch, tmp_path):
    _patch_worktree_tmpdir(monkeypatch, tmp_path)
    fake_run = _FakeRun()
    monkeypatch.setattr(ah, "run", fake_run)
    # don't actually sleep on backoff
    monkeypatch.setattr(ah.time, "sleep", lambda *_a, **_k: None)

    attempts = {"n": 0}

    def flaky_push(cmd, cwd, capture_output, text):
        attempts["n"] += 1
        rc = 0 if attempts["n"] >= 3 else 1
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="rejected")

    monkeypatch.setattr(ah.subprocess, "run", flaky_push)

    ah.append_history([make_envelope()])

    assert attempts["n"] == 3  # failed twice, succeeded on third


def test_append_history_exits_after_max_retries(monkeypatch, tmp_path):
    _patch_worktree_tmpdir(monkeypatch, tmp_path)
    monkeypatch.setattr(ah, "run", _FakeRun())
    monkeypatch.setattr(ah.time, "sleep", lambda *_a, **_k: None)

    attempts = {"n": 0}

    def always_fail(cmd, cwd, capture_output, text):
        attempts["n"] += 1
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(ah.subprocess, "run", always_fail)

    with pytest.raises(SystemExit) as excinfo:
        ah.append_history([make_envelope()])

    assert attempts["n"] == ah.MAX_PUSH_RETRIES
    assert "push failed after" in str(excinfo.value)


def test_append_history_worktree_cleanup_runs_on_success(monkeypatch, tmp_path):
    _patch_worktree_tmpdir(monkeypatch, tmp_path)
    fake_run = _FakeRun()
    monkeypatch.setattr(ah, "run", fake_run)
    monkeypatch.setattr(
        ah.subprocess,
        "run",
        lambda cmd, cwd, capture_output, text: subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    ah.append_history([make_envelope()])

    # the finally block must invoke `git worktree remove --force <dir>`
    assert any(
        c[:4] == ["git", "worktree", "remove", "--force"] for c in fake_run.calls
    )


def test_append_history_commit_uses_build_commit_message(monkeypatch, tmp_path):
    _patch_worktree_tmpdir(monkeypatch, tmp_path)
    fake_run = _FakeRun()
    monkeypatch.setattr(ah, "run", fake_run)
    monkeypatch.setattr(
        ah.subprocess,
        "run",
        lambda cmd, cwd, capture_output, text: subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    env = make_envelope(
        package_name="solo_pkg",
        record=make_record(sweep_kind="build", status="pass", autoware_version="9.9.9"),
    )
    ah.append_history([env])

    commit_calls = [c for c in fake_run.calls if c[:2] == ["git", "commit"]]
    assert len(commit_calls) == 1
    # ["git","commit","-m",<message>]
    assert commit_calls[0][-1] == ah.build_commit_message([env])
    assert "for solo_pkg @ jazzy" in commit_calls[0][-1]


def test_run_helper_invokes_subprocess(monkeypatch, tmp_path):
    captured = {}

    def fake_subprocess_run(cmd, cwd, check, capture_output, text):
        captured.update(cmd=cmd, cwd=cwd, check=check,
                        capture_output=capture_output, text=text)
        return subprocess.CompletedProcess(cmd, 0, "out", "")

    monkeypatch.setattr(ah.subprocess, "run", fake_subprocess_run)
    result = ah.run(["git", "status"], tmp_path)

    assert captured["cmd"] == ["git", "status"]
    assert captured["cwd"] == tmp_path
    assert captured["check"] is True
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert result.stdout == "out"
