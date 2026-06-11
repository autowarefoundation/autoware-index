"""Tests for scripts/append_history.py.

Covers the pure parts (load_json, build_commit_message) against real temp
files / stdin, exercises write_outputs against a REAL tmp dir (it is pure file
routing: history appends, state cursors, metadata merge), and exercises
append_history's control flow by monkeypatching the git/push side effects
(no real git, no real pushes).

NOTE: the module docstring describes the history files as NDJSON, but the
--records/--states inputs are single JSON arrays read with json.load. Tests
assert the REAL behavior.
"""

from __future__ import annotations

import io
import json
import subprocess

import pytest

import append_history as ah


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_record(
    sweep_kind="build",
    status="pass",
    autoware_version="1.0.0",
    repo_name="autoware_demo_repo",
):
    record = {
        "sweep_kind": sweep_kind,
        "status": status,
        "autoware_version": autoware_version,
    }
    # v2 records carry the hosting repository entry's name; repo_name=None
    # builds a v1-style record without it (commit message must fall back).
    if repo_name is not None:
        record["repo_name"] = repo_name
    return record


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


def make_state(ros_distro="jazzy", repo_name="autoware_demo_repo", state=None):
    return {
        "ros_distro": ros_distro,
        "repo_name": repo_name,
        "state": state if state is not None else {"url": "https://example.com/r"},
    }


# ---------------------------------------------------------------------------
# load_json
# ---------------------------------------------------------------------------

def test_load_json_from_file(tmp_path):
    envelopes = [make_envelope(package_name="pkg_a"), make_envelope(package_name="pkg_b")]
    src = tmp_path / "records.json"
    src.write_text(json.dumps(envelopes), encoding="utf-8")

    result = ah.load_json(str(src), "records")

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["package_name"] == "pkg_a"
    assert result[1]["package_name"] == "pkg_b"
    assert result[0]["record"]["sweep_kind"] == "build"


def test_load_json_empty_array_from_file(tmp_path):
    src = tmp_path / "empty.json"
    src.write_text("[]", encoding="utf-8")

    assert ah.load_json(str(src), "records") == []


def test_load_json_from_stdin(monkeypatch):
    envelopes = [make_envelope(ros_distro="humble", package_name="pkg_stdin")]
    monkeypatch.setattr(ah.sys, "stdin", io.StringIO(json.dumps(envelopes)))

    result = ah.load_json("-", "records")

    assert isinstance(result, list)
    assert result == envelopes


def test_load_json_non_list_exits(tmp_path):
    # A JSON object (not an array) must be rejected via sys.exit; the message
    # names which input was malformed via the `what` label.
    src = tmp_path / "obj.json"
    src.write_text(json.dumps({"ros_distro": "jazzy"}), encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        ah.load_json(str(src), "records")
    # sys.exit("msg") carries the message as the exit code value.
    assert "records JSON must be an array" in str(excinfo.value)


def test_load_json_error_carries_states_label(tmp_path):
    src = tmp_path / "obj.json"
    src.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        ah.load_json(str(src), "states")
    assert "states JSON must be an array" in str(excinfo.value)


def test_load_json_non_list_from_stdin_exits(monkeypatch):
    monkeypatch.setattr(ah.sys, "stdin", io.StringIO('"just a string"'))
    with pytest.raises(SystemExit):
        ah.load_json("-", "records")


def test_load_json_missing_file_raises(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(FileNotFoundError):
        ah.load_json(str(missing), "records")


def test_load_json_invalid_json_raises(tmp_path):
    src = tmp_path / "bad.json"
    src.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        ah.load_json(str(src), "records")


# ---------------------------------------------------------------------------
# build_commit_message
# ---------------------------------------------------------------------------

def test_build_commit_message_single():
    env = make_envelope(
        ros_distro="jazzy",
        package_name="autoware_livox_tag_filter",
        record=make_record(
            sweep_kind="release",
            status="fail",
            autoware_version="2.3.4",
            repo_name="livox_tag_filter_repo",
        ),
    )
    msg = ah.build_commit_message([env])
    assert msg == (
        "chore(data): release sweep fail "
        "for autoware_livox_tag_filter (livox_tag_filter_repo) @ jazzy "
        "/ autoware 2.3.4"
    )


def test_build_commit_message_single_without_repo_name_falls_back():
    # v1-style record (no repo_name) -> the package name doubles as the repo.
    env = make_envelope(package_name="solo_pkg", record=make_record(repo_name=None))
    msg = ah.build_commit_message([env])
    assert "for solo_pkg (solo_pkg) @ jazzy" in msg


def test_build_commit_message_multiple_summarizes_and_sorts():
    envelopes = [
        make_envelope(ros_distro="rolling", record=make_record(sweep_kind="release", repo_name="repo_b")),
        make_envelope(ros_distro="humble", record=make_record(sweep_kind="build", repo_name="repo_a")),
        make_envelope(ros_distro="humble", record=make_record(sweep_kind="build", repo_name="repo_a")),
    ]
    msg = ah.build_commit_message(envelopes)
    # count is total envelopes (3); kinds, repos, and distros deduped + sorted.
    assert msg == (
        "chore(data): append 3 sweep result(s) "
        "[build,release] for repo_a,repo_b across humble,rolling"
    )


def test_build_commit_message_multiple_dedupes_kinds_repos_and_distros():
    envelopes = [
        make_envelope(ros_distro="jazzy", record=make_record(sweep_kind="build", repo_name="repo_a")),
        make_envelope(ros_distro="jazzy", record=make_record(sweep_kind="build", repo_name="repo_a")),
    ]
    msg = ah.build_commit_message(envelopes)
    assert msg == (
        "chore(data): append 2 sweep result(s) [build] for repo_a across jazzy"
    )


def test_build_commit_message_multiple_repo_name_fallback():
    # A v1-style record without repo_name contributes its package name to the
    # repo list instead of crashing the summary.
    envelopes = [
        make_envelope(package_name="v2_pkg", record=make_record(repo_name="real_repo")),
        make_envelope(package_name="v1_pkg", record=make_record(repo_name=None)),
    ]
    msg = ah.build_commit_message(envelopes)
    assert "for real_repo,v1_pkg across jazzy" in msg


# ---------------------------------------------------------------------------
# write_outputs -- pure file routing, exercised against a real tmp dir
# ---------------------------------------------------------------------------

def test_write_outputs_appends_one_history_line_per_envelope(tmp_path):
    envelopes = [
        make_envelope(ros_distro="jazzy", package_name="pkg_a", record=make_record(status="pass")),
        make_envelope(ros_distro="humble", package_name="pkg_b", record=make_record(status="fail")),
    ]
    ah.write_outputs(tmp_path, envelopes, [], None)

    f_a = tmp_path / "history" / "jazzy" / "pkg_a.ndjson"
    f_b = tmp_path / "history" / "humble" / "pkg_b.ndjson"
    assert f_a.exists() and f_b.exists()

    lines_a = f_a.read_text(encoding="utf-8").splitlines()
    assert len(lines_a) == 1
    # compact json, one record per line, round-trippable
    assert json.loads(lines_a[0]) == make_record(status="pass")
    # compact separators -> no spaces after ':' or ','
    assert ", " not in lines_a[0]
    assert json.loads(f_b.read_text(encoding="utf-8")) == make_record(status="fail")


def test_write_outputs_appends_without_truncating(tmp_path):
    # pre-seed an existing line; appended write must not truncate it
    target = tmp_path / "history" / "jazzy" / "pkg_a.ndjson"
    target.parent.mkdir(parents=True)
    target.write_text('{"pre":"existing"}\n', encoding="utf-8")

    envelopes = [
        make_envelope(ros_distro="jazzy", package_name="pkg_a", record=make_record(status="r1")),
        make_envelope(ros_distro="jazzy", package_name="pkg_a", record=make_record(status="r2")),
    ]
    ah.write_outputs(tmp_path, envelopes, [], None)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"pre":"existing"}'
    assert json.loads(lines[1])["status"] == "r1"
    assert json.loads(lines[2])["status"] == "r2"
    assert len(lines) == 3


def test_write_outputs_writes_state_file_per_repo(tmp_path):
    states = [
        make_state(ros_distro="jazzy", repo_name="repo_a",
                   state={"url": "u", "ref": {"kind": "tag", "value": "1"}}),
        make_state(ros_distro="humble", repo_name="repo_b", state={"url": "v"}),
    ]
    ah.write_outputs(tmp_path, [], states, None)

    f_a = tmp_path / "state" / "jazzy" / "repo_a.json"
    f_b = tmp_path / "state" / "humble" / "repo_b.json"
    # the file holds exactly entry["state"]: pretty-printed + trailing newline
    assert f_a.read_text(encoding="utf-8") == json.dumps(states[0]["state"], indent=2) + "\n"
    assert json.loads(f_b.read_text(encoding="utf-8")) == {"url": "v"}


def test_write_outputs_state_files_overwrite_not_append(tmp_path):
    # State is a mutable cursor: a later write REPLACES the file content.
    ah.write_outputs(tmp_path, [], [make_state(state={"gen": 1})], None)
    ah.write_outputs(tmp_path, [], [make_state(state={"gen": 2})], None)
    f = tmp_path / "state" / "jazzy" / "autoware_demo_repo.json"
    assert json.loads(f.read_text(encoding="utf-8")) == {"gen": 2}


def test_write_outputs_never_regresses_a_newer_state_cursor(tmp_path, capsys):
    # Out-of-order record jobs: run B (newer registration) records first, then
    # the slower run A (older registration) lands. A's history lines still
    # append, but A must NOT roll the cursor back over B's — otherwise the
    # level-triggered discover would re-sweep the old ref and the site would
    # show stale state until the next sweep.
    newer = make_state(state={"url": "u", "ref": {"kind": "tag", "value": "2.0"}, "at": "2026-06-11T12:00:00Z"})
    older = make_state(state={"url": "u", "ref": {"kind": "tag", "value": "1.0"}, "at": "2026-06-11T11:00:00Z"})
    ah.write_outputs(tmp_path, [], [newer], None)
    ah.write_outputs(tmp_path, [], [older], None)

    f = tmp_path / "state" / "jazzy" / "autoware_demo_repo.json"
    assert json.loads(f.read_text(encoding="utf-8"))["ref"]["value"] == "2.0"
    assert "not regressing" in capsys.readouterr().err

    # The reverse order (older first) does advance.
    ah.write_outputs(tmp_path, [], [older], None)  # idempotent: still newer on disk
    assert json.loads(f.read_text(encoding="utf-8"))["ref"]["value"] == "2.0"


def test_write_outputs_corrupt_existing_state_is_overwritten(tmp_path):
    f = tmp_path / "state" / "jazzy" / "autoware_demo_repo.json"
    f.parent.mkdir(parents=True)
    f.write_text("{not json", encoding="utf-8")
    fresh = make_state(state={"url": "u", "at": "2026-06-11T12:00:00Z"})
    ah.write_outputs(tmp_path, [], [fresh], None)
    assert json.loads(f.read_text(encoding="utf-8"))["url"] == "u"


def test_write_outputs_merges_metadata_over_existing_tree(tmp_path):
    # A sweep stages only what it swept; the merge must never delete the
    # other packages' cached files (the rmtree+copytree regression).
    staged = tmp_path / "staged"
    (staged / "jazzy").mkdir(parents=True)
    (staged / "jazzy" / "swept.xml").write_text("FRESH")

    worktree = tmp_path / "worktree"
    (worktree / "metadata" / "jazzy").mkdir(parents=True)
    (worktree / "metadata" / "jazzy" / "swept.xml").write_text("STALE")
    (worktree / "metadata" / "jazzy" / "other.xml").write_text("KEEP")
    (worktree / "metadata" / "humble").mkdir(parents=True)
    (worktree / "metadata" / "humble" / "third.xml").write_text("KEEP")

    ah.write_outputs(worktree, [], [], staged)

    assert (worktree / "metadata" / "jazzy" / "swept.xml").read_text() == "FRESH"
    assert (worktree / "metadata" / "jazzy" / "other.xml").read_text() == "KEEP"
    assert (worktree / "metadata" / "humble" / "third.xml").read_text() == "KEEP"


def test_write_outputs_metadata_none_or_missing_dir_skipped(tmp_path):
    ah.write_outputs(tmp_path, [], [], None)
    assert not (tmp_path / "metadata").exists()
    ah.write_outputs(tmp_path, [], [], tmp_path / "does-not-exist")
    assert not (tmp_path / "metadata").exists()


# ---------------------------------------------------------------------------
# append_history -- side effects monkeypatched
# ---------------------------------------------------------------------------

def test_append_history_empty_is_noop(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ah, "run", lambda *a, **k: calls.append(a))

    # empty envelopes short-circuit even when states are non-empty: state
    # must never advance without its records landing.
    ah.append_history([], [make_state()], None)

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
    ah.append_history(envelopes, [], None)

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

    # exactly one push happened, to HEAD:data
    assert len(pushes) == 1
    assert pushes[0] == ["git", "push", "origin", "HEAD:data"]


def test_append_history_stages_with_git_add_dash_a(monkeypatch, tmp_path):
    # history/, state/, and metadata/ must land in ONE commit, so the stage
    # step is `git add -A` (no longer `git add history/`).
    _patch_worktree_tmpdir(monkeypatch, tmp_path)
    fake_run = _FakeRun()
    monkeypatch.setattr(ah, "run", fake_run)
    monkeypatch.setattr(
        ah.subprocess,
        "run",
        lambda cmd, cwd, capture_output, text: subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    ah.append_history([make_envelope()], [make_state()], None)

    assert ["git", "add", "-A"] in fake_run.calls
    assert not any(
        c[:2] == ["git", "add"] and c != ["git", "add", "-A"] for c in fake_run.calls
    )


def test_append_history_writes_state_files_alongside_history(monkeypatch, tmp_path, capsys):
    worktree = _patch_worktree_tmpdir(monkeypatch, tmp_path)
    monkeypatch.setattr(ah, "run", _FakeRun())
    monkeypatch.setattr(
        ah.subprocess,
        "run",
        lambda cmd, cwd, capture_output, text: subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    states = [make_state(ros_distro="jazzy", repo_name="repo_a", state={"k": "v"})]
    ah.append_history([make_envelope()], states, None)

    state_file = worktree / "state" / "jazzy" / "repo_a.json"
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"k": "v"}
    # the push log reports both counts
    assert "1 history record(s), 1 state advance(s)" in capsys.readouterr().err


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
    ah.append_history(envelopes, [], None)

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

    ah.append_history([make_envelope()], [], None)
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

    ah.append_history([make_envelope()], [], None)

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
        ah.append_history([make_envelope()], [], None)

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

    ah.append_history([make_envelope()], [], None)

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
        record=make_record(sweep_kind="build", status="pass", autoware_version="9.9.9",
                           repo_name="solo_repo"),
    )
    ah.append_history([env], [], None)

    commit_calls = [c for c in fake_run.calls if "commit" in c]
    assert len(commit_calls) == 1
    # ["git","-c","user.name=...","-c","user.email=...","commit","-m",<message>]
    # — identity is per-command so a local run never rewrites the shared
    # repo config with the bot's name.
    assert f"user.name={ah.BOT_NAME}" in commit_calls[0]
    assert f"user.email={ah.BOT_EMAIL}" in commit_calls[0]
    assert not any(c[:2] == ["git", "config"] for c in fake_run.calls)
    assert commit_calls[0][-1] == ah.build_commit_message([env])
    assert "for solo_pkg (solo_repo) @ jazzy" in commit_calls[0][-1]


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


# ---------------------------------------------------------------------------
# main -- CLI arg routing into append_history
# ---------------------------------------------------------------------------

def test_main_passes_records_states_and_metadata_dir(monkeypatch, tmp_path):
    records = tmp_path / "records.json"
    records.write_text(json.dumps([make_envelope()]), encoding="utf-8")
    states = tmp_path / "states.json"
    states.write_text(json.dumps([make_state()]), encoding="utf-8")
    metadata = tmp_path / "staged-metadata"
    metadata.mkdir()

    captured = {}
    monkeypatch.setattr(
        ah, "append_history",
        lambda envelopes, st, md: captured.update(envelopes=envelopes, states=st, metadata=md),
    )
    monkeypatch.setattr(ah.sys, "argv", [
        "append_history.py",
        "--records", str(records),
        "--states", str(states),
        "--metadata-dir", str(metadata),
    ])

    ah.main()

    assert captured["envelopes"] == [make_envelope()]
    assert captured["states"] == [make_state()]
    assert captured["metadata"] == metadata


def test_main_states_and_metadata_are_optional(monkeypatch, tmp_path):
    records = tmp_path / "records.json"
    records.write_text(json.dumps([make_envelope()]), encoding="utf-8")

    captured = {}
    monkeypatch.setattr(
        ah, "append_history",
        lambda envelopes, st, md: captured.update(envelopes=envelopes, states=st, metadata=md),
    )
    monkeypatch.setattr(ah.sys, "argv", ["append_history.py", "--records", str(records)])

    ah.main()

    assert captured["envelopes"] == [make_envelope()]
    assert captured["states"] == []
    assert captured["metadata"] is None
