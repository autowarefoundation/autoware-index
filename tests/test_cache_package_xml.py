"""Tests for scripts/cache_package_xml.py (metadata caching, Step 4.1).

Focus is the pure parts:
  - find_package_xml: picks the package.xml whose <name> matches, not the first.
  - rows_from_matrix: parses the sweep-matrix JSON into normalized rows.
  - rows_from_distributions: yields a row per registered package.
  - commit_message: pure formatting.

The git/network parts (checkout / cache_one / push_metadata) are exercised with
the side-effecting calls monkeypatched, so no real clones or pushes happen.
"""

import json

import pytest

import cache_package_xml as m


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def write_package_xml(directory, name, description="desc", fmt="3"):
    """Write a minimal but realistic package.xml with <name> into `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "package.xml"
    path.write_text(
        f'<?xml version="1.0"?>\n'
        f'<package format="{fmt}">\n'
        f"  <name>{name}</name>\n"
        f"  <description>{description}</description>\n"
        f"</package>\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------
# find_package_xml
# --------------------------------------------------------------------------
def test_find_package_xml_picks_matching_name_not_first(tmp_path):
    # Three packages in one source tree; sorted glob would hit alpha first.
    write_package_xml(tmp_path / "alpha", "alpha_pkg")
    write_package_xml(tmp_path / "beta", "beta_pkg")
    target = write_package_xml(tmp_path / "gamma", "gamma_pkg")

    found = m.find_package_xml(tmp_path, "gamma_pkg")
    assert found == target


def test_find_package_xml_returns_first_match_in_sorted_order(tmp_path):
    # The matched <name> lives in a dir that does not sort first.
    write_package_xml(tmp_path / "z_dir", "wanted")
    found = m.find_package_xml(tmp_path, "wanted")
    assert found == tmp_path / "z_dir" / "package.xml"


def test_find_package_xml_no_match_returns_none(tmp_path):
    write_package_xml(tmp_path / "alpha", "alpha_pkg")
    write_package_xml(tmp_path / "beta", "beta_pkg")
    assert m.find_package_xml(tmp_path, "does_not_exist") is None


def test_find_package_xml_empty_tree_returns_none(tmp_path):
    assert m.find_package_xml(tmp_path, "anything") is None


def test_find_package_xml_strips_whitespace_around_name(tmp_path):
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "package.xml").write_text(
        '<?xml version="1.0"?>\n'
        "<package>\n"
        "  <name>\n    spaced_pkg\n  </name>\n"
        "</package>\n",
        encoding="utf-8",
    )
    assert m.find_package_xml(tmp_path, "spaced_pkg") == d / "package.xml"


def test_find_package_xml_skips_malformed_xml_and_finds_valid(tmp_path):
    # A broken package.xml that sorts before the good one must not crash;
    # the valid match should still be returned.
    bad = tmp_path / "aaa_bad"
    bad.mkdir()
    (bad / "package.xml").write_text("<package><name>oops", encoding="utf-8")
    good = write_package_xml(tmp_path / "zzz_good", "good_pkg")
    assert m.find_package_xml(tmp_path, "good_pkg") == good


def test_find_package_xml_malformed_only_returns_none(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "package.xml").write_text("<not valid xml", encoding="utf-8")
    assert m.find_package_xml(tmp_path, "anything") is None


def test_find_package_xml_no_name_element_skipped(tmp_path):
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "package.xml").write_text(
        '<?xml version="1.0"?>\n<package><description>x</description></package>\n',
        encoding="utf-8",
    )
    assert m.find_package_xml(tmp_path, "pkg") is None


def test_find_package_xml_nested_deeply(tmp_path):
    target = write_package_xml(tmp_path / "a" / "b" / "c" / "deep_pkg", "deep_pkg")
    assert m.find_package_xml(tmp_path, "deep_pkg") == target


# --------------------------------------------------------------------------
# rows_from_matrix
# --------------------------------------------------------------------------
def test_rows_from_matrix_normalizes_fields(tmp_path):
    matrix = {
        "include": [
            {
                "ros_distro": "humble",
                "package_name": "pkg_a",
                "package_repository": "https://example.com/a.git",
                "ref_kind": "tag",
                "ref_value": "v1.2.3",
            }
        ]
    }
    mf = tmp_path / "matrix.json"
    mf.write_text(json.dumps(matrix))

    rows = m.rows_from_matrix(mf)
    assert rows == [
        {
            "distro": "humble",
            "package": "pkg_a",
            "repository": "https://example.com/a.git",
            "kind": "tag",
            "value": "v1.2.3",
        }
    ]


def test_rows_from_matrix_defaults_kind_to_branch(tmp_path):
    matrix = {
        "include": [
            {
                "ros_distro": "jazzy",
                "package_name": "pkg_b",
                "package_repository": "repo_b",
                "ref_value": "main",
            }
        ]
    }
    mf = tmp_path / "matrix.json"
    mf.write_text(json.dumps(matrix))

    rows = m.rows_from_matrix(mf)
    assert rows[0]["kind"] == "branch"
    assert rows[0]["value"] == "main"


def test_rows_from_matrix_empty_include(tmp_path):
    mf = tmp_path / "matrix.json"
    mf.write_text(json.dumps({"include": []}))
    assert m.rows_from_matrix(mf) == []


def test_rows_from_matrix_missing_include_key(tmp_path):
    mf = tmp_path / "matrix.json"
    mf.write_text(json.dumps({}))
    assert m.rows_from_matrix(mf) == []


def test_rows_from_matrix_multiple_rows_preserve_order(tmp_path):
    matrix = {
        "include": [
            {
                "ros_distro": "humble",
                "package_name": "p1",
                "package_repository": "r1",
                "ref_value": "v1",
            },
            {
                "ros_distro": "jazzy",
                "package_name": "p2",
                "package_repository": "r2",
                "ref_kind": "sha",
                "ref_value": "abc123",
            },
        ]
    }
    mf = tmp_path / "matrix.json"
    mf.write_text(json.dumps(matrix))

    rows = m.rows_from_matrix(mf)
    assert [r["package"] for r in rows] == ["p1", "p2"]
    assert rows[1]["kind"] == "sha"


def test_rows_from_matrix_missing_required_key_raises(tmp_path):
    # package_name is required (direct key access); a missing one is a hard error.
    matrix = {"include": [{"ros_distro": "humble", "package_repository": "r", "ref_value": "v"}]}
    mf = tmp_path / "matrix.json"
    mf.write_text(json.dumps(matrix))
    with pytest.raises(KeyError):
        m.rows_from_matrix(mf)


# --------------------------------------------------------------------------
# rows_from_distributions
# --------------------------------------------------------------------------
def write_distribution(dirpath, filename, content):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / filename).write_text(content)


def test_rows_from_distributions_one_package(tmp_path):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
ros_distro: humble
packages:
  my_pkg:
    repository: https://example.com/repo.git
    ref:
      kind: tag
      value: 1.0.0
""",
    )
    rows = m.rows_from_distributions(tmp_path)
    assert rows == [
        {
            "distro": "humble",
            "package": "my_pkg",
            "repository": "https://example.com/repo.git",
            "kind": "tag",
            "value": "1.0.0",
        }
    ]


def test_rows_from_distributions_defaults_distro_to_stem(tmp_path):
    # No ros_distro key -> the file stem is used as the distro.
    write_distribution(
        tmp_path,
        "jazzy.yaml",
        """
packages:
  pkg:
    repository: repo
    ref:
      value: main
""",
    )
    rows = m.rows_from_distributions(tmp_path)
    assert rows[0]["distro"] == "jazzy"


def test_rows_from_distributions_defaults_kind_to_branch(tmp_path):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
ros_distro: humble
packages:
  pkg:
    repository: repo
    ref:
      value: somebranch
""",
    )
    rows = m.rows_from_distributions(tmp_path)
    assert rows[0]["kind"] == "branch"
    assert rows[0]["value"] == "somebranch"


def test_rows_from_distributions_skips_missing_repository(tmp_path, capsys):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
ros_distro: humble
packages:
  no_repo:
    ref:
      value: main
  good:
    repository: repo
    ref:
      value: main
""",
    )
    rows = m.rows_from_distributions(tmp_path)
    assert [r["package"] for r in rows] == ["good"]
    err = capsys.readouterr().err
    assert "no_repo" in err
    assert "missing repository/ref" in err


def test_rows_from_distributions_skips_missing_value(tmp_path, capsys):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
ros_distro: humble
packages:
  no_value:
    repository: repo
""",
    )
    rows = m.rows_from_distributions(tmp_path)
    assert rows == []
    assert "no_value" in capsys.readouterr().err


def test_rows_from_distributions_multiple_files_and_packages(tmp_path):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
ros_distro: humble
packages:
  a:
    repository: ra
    ref:
      value: va
  b:
    repository: rb
    ref:
      value: vb
""",
    )
    write_distribution(
        tmp_path,
        "jazzy.yaml",
        """
ros_distro: jazzy
packages:
  c:
    repository: rc
    ref:
      value: vc
""",
    )
    rows = m.rows_from_distributions(tmp_path)
    # files iterated sorted by name; within a file, dict order preserved.
    assert [(r["distro"], r["package"]) for r in rows] == [
        ("humble", "a"),
        ("humble", "b"),
        ("jazzy", "c"),
    ]


def test_rows_from_distributions_empty_dir(tmp_path):
    assert m.rows_from_distributions(tmp_path) == []


def test_rows_from_distributions_empty_yaml_file(tmp_path):
    write_distribution(tmp_path, "humble.yaml", "")
    # Empty doc -> no packages, no crash.
    assert m.rows_from_distributions(tmp_path) == []


def test_rows_from_distributions_null_spec_skipped(tmp_path, capsys):
    # A package mapped to null (spec is None) must not crash; missing repo/ref.
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
ros_distro: humble
packages:
  nullpkg:
""",
    )
    assert m.rows_from_distributions(tmp_path) == []
    assert "nullpkg" in capsys.readouterr().err


def test_rows_from_distributions_ignores_non_yaml(tmp_path):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
ros_distro: humble
packages:
  a:
    repository: ra
    ref:
      value: va
""",
    )
    (tmp_path / "README.md").write_text("not a distribution")
    (tmp_path / "notes.txt").write_text("ignore me")
    rows = m.rows_from_distributions(tmp_path)
    assert [r["package"] for r in rows] == ["a"]


# --------------------------------------------------------------------------
# commit_message
# --------------------------------------------------------------------------
def test_commit_message_single_row():
    rows = [{"distro": "humble", "package": "my_pkg"}]
    assert m.commit_message(rows) == "chore(data): cache package.xml for my_pkg @ humble"


def test_commit_message_multiple_rows_same_distro():
    rows = [
        {"distro": "humble", "package": "a"},
        {"distro": "humble", "package": "b"},
    ]
    assert m.commit_message(rows) == "chore(data): cache 2 package.xml file(s) across humble"


def test_commit_message_multiple_rows_distros_sorted_and_unique():
    rows = [
        {"distro": "jazzy", "package": "a"},
        {"distro": "humble", "package": "b"},
        {"distro": "jazzy", "package": "c"},
    ]
    # 3 rows, distros deduped + sorted -> "humble,jazzy".
    assert m.commit_message(rows) == "chore(data): cache 3 package.xml file(s) across humble,jazzy"


# --------------------------------------------------------------------------
# checkout (git shelled out -> monkeypatch m.run)
# --------------------------------------------------------------------------
def _fake_completed(returncode=0, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_checkout_branch_uses_shallow_clone(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, cwd=None, check=True):
        calls.append((cmd, cwd, check))
        return _fake_completed(0)

    monkeypatch.setattr(m, "run", fake_run)
    ok = m.checkout("https://example.com/r.git", "branch", "main", tmp_path / "dest")
    assert ok is True
    assert len(calls) == 1
    cmd = calls[0][0]
    assert cmd[:2] == ["git", "clone"]
    assert "--depth" in cmd and "1" in cmd
    assert "--branch" in cmd
    assert cmd[cmd.index("--branch") + 1] == "main"


def test_checkout_tag_uses_branch_flag(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, cwd=None, check=True):
        seen["cmd"] = cmd
        return _fake_completed(0)

    monkeypatch.setattr(m, "run", fake_run)
    assert m.checkout("repo", "tag", "v2.0", tmp_path / "d") is True
    assert "--branch" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--branch") + 1] == "v2.0"


def test_checkout_branch_clone_failure_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "run", lambda *a, **k: _fake_completed(128, stderr="boom"))
    assert m.checkout("repo", "branch", "main", tmp_path / "d") is False


def test_checkout_sha_clones_then_checks_out(monkeypatch, tmp_path):
    cmds = []

    def fake_run(cmd, cwd=None, check=True):
        cmds.append(cmd)
        return _fake_completed(0)

    monkeypatch.setattr(m, "run", fake_run)
    ok = m.checkout("repo", "sha", "deadbeef", tmp_path / "d")
    assert ok is True
    # First a blobless clone (no --branch), then a checkout of the sha.
    assert cmds[0][:2] == ["git", "clone"]
    assert "--branch" not in cmds[0]
    assert cmds[1][:2] == ["git", "checkout"]
    assert cmds[1][2] == "deadbeef"


def test_checkout_sha_clone_failure_returns_false(monkeypatch, tmp_path):
    def fake_run(cmd, cwd=None, check=True):
        return _fake_completed(1)  # clone fails

    monkeypatch.setattr(m, "run", fake_run)
    assert m.checkout("repo", "sha", "deadbeef", tmp_path / "d") is False


def test_checkout_sha_checkout_failure_returns_false(monkeypatch, tmp_path):
    results = iter([_fake_completed(0), _fake_completed(1)])  # clone ok, checkout fails

    def fake_run(cmd, cwd=None, check=True):
        return next(results)

    monkeypatch.setattr(m, "run", fake_run)
    assert m.checkout("repo", "sha", "deadbeef", tmp_path / "d") is False


# --------------------------------------------------------------------------
# cache_one (checkout monkeypatched; real find_package_xml + copy)
# --------------------------------------------------------------------------
def test_cache_one_success_copies_package_xml(monkeypatch, tmp_path):
    def fake_checkout(repository, kind, value, dest):
        # Populate the temp checkout dir with a matching package.xml.
        write_package_xml(dest / "src" / "my_pkg", "my_pkg", description="Hello")
        return True

    monkeypatch.setattr(m, "checkout", fake_checkout)
    out = tmp_path / "out"
    row = {"distro": "humble", "package": "my_pkg", "repository": "r", "kind": "branch", "value": "main"}
    assert m.cache_one(row, out) is True

    cached = out / "humble" / "my_pkg.xml"
    assert cached.exists()
    assert "Hello" in cached.read_text()


def test_cache_one_checkout_failure_returns_false(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(m, "checkout", lambda *a, **k: False)
    out = tmp_path / "out"
    row = {"distro": "humble", "package": "p", "repository": "r", "kind": "branch", "value": "main"}
    assert m.cache_one(row, out) is False
    assert "could not check out" in capsys.readouterr().err
    assert not (out / "humble" / "p.xml").exists()


def test_cache_one_no_matching_package_xml_returns_false(monkeypatch, tmp_path, capsys):
    def fake_checkout(repository, kind, value, dest):
        write_package_xml(dest / "other", "some_other_pkg")
        return True

    monkeypatch.setattr(m, "checkout", fake_checkout)
    out = tmp_path / "out"
    row = {"distro": "humble", "package": "wanted_pkg", "repository": "r", "kind": "branch", "value": "main"}
    assert m.cache_one(row, out) is False
    err = capsys.readouterr().err
    assert "no package.xml" in err
    assert "wanted_pkg" in err


def test_cache_one_picks_matching_among_many(monkeypatch, tmp_path):
    def fake_checkout(repository, kind, value, dest):
        write_package_xml(dest / "a", "alpha", description="ALPHA")
        write_package_xml(dest / "b", "beta", description="BETA")
        return True

    monkeypatch.setattr(m, "checkout", fake_checkout)
    out = tmp_path / "out"
    row = {"distro": "humble", "package": "beta", "repository": "r", "kind": "branch", "value": "main"}
    assert m.cache_one(row, out) is True
    assert "BETA" in (out / "humble" / "beta.xml").read_text()


def test_cache_one_cleans_up_tempdir(monkeypatch, tmp_path):
    captured = {}

    def fake_checkout(repository, kind, value, dest):
        captured["dest"] = dest
        write_package_xml(dest / "p", "p")
        return True

    monkeypatch.setattr(m, "checkout", fake_checkout)
    out = tmp_path / "out"
    row = {"distro": "humble", "package": "p", "repository": "r", "kind": "branch", "value": "main"}
    assert m.cache_one(row, out) is True
    # The temp checkout dir is removed in the finally block.
    assert not captured["dest"].exists()


# --------------------------------------------------------------------------
# push_metadata (all git side effects monkeypatched)
# --------------------------------------------------------------------------
def test_push_metadata_commits_and_pushes_on_first_attempt(monkeypatch, tmp_path):
    staged = tmp_path / "staged"
    (staged / "humble").mkdir(parents=True)
    (staged / "humble" / "p.xml").write_text("<package/>")

    monkeypatch.chdir(tmp_path)
    commands = []

    def fake_run(cmd, cwd=None, check=True):
        commands.append(cmd)
        # status --porcelain must report a change so a commit happens.
        if cmd[:2] == ["git", "status"]:
            return _fake_completed(0, stdout=" M metadata/humble/p.xml\n")
        return _fake_completed(0)

    # worktree add would normally create tmpdir; copytree needs it to exist.
    def fake_worktree_aware_run(cmd, cwd=None, check=True):
        if cmd[:3] == ["git", "worktree", "add"]:
            # cmd = [git, worktree, add, <tmpdir>, origin/data]
            from pathlib import Path as _P

            _P(cmd[3]).mkdir(parents=True, exist_ok=True)
        return fake_run(cmd, cwd, check)

    monkeypatch.setattr(m, "run", fake_worktree_aware_run)

    rows = [{"distro": "humble", "package": "p"}]
    m.push_metadata(staged, rows)

    joined = [" ".join(c) for c in commands]
    assert any(c.startswith("git fetch origin data") for c in joined)
    assert any(c.startswith("git commit") for c in joined)
    assert any(c.startswith("git push origin HEAD:data") for c in joined)


def test_push_metadata_no_changes_skips_commit(monkeypatch, tmp_path):
    staged = tmp_path / "staged"
    staged.mkdir()
    monkeypatch.chdir(tmp_path)
    commands = []

    def fake_run(cmd, cwd=None, check=True):
        commands.append(cmd)
        if cmd[:3] == ["git", "worktree", "add"]:
            from pathlib import Path as _P

            _P(cmd[3]).mkdir(parents=True, exist_ok=True)
        if cmd[:2] == ["git", "status"]:
            return _fake_completed(0, stdout="")  # nothing staged
        return _fake_completed(0)

    monkeypatch.setattr(m, "run", fake_run)
    rows = [{"distro": "humble", "package": "p"}]
    m.push_metadata(staged, rows)

    joined = [" ".join(c) for c in commands]
    assert not any(c.startswith("git commit") for c in joined)
    assert not any(c.startswith("git push") for c in joined)


def test_push_metadata_retries_then_succeeds(monkeypatch, tmp_path):
    staged = tmp_path / "staged"
    staged.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)  # no real backoff

    push_attempts = {"n": 0}

    def fake_run(cmd, cwd=None, check=True):
        if cmd[:3] == ["git", "worktree", "add"]:
            from pathlib import Path as _P

            _P(cmd[3]).mkdir(parents=True, exist_ok=True)
        if cmd[:2] == ["git", "status"]:
            return _fake_completed(0, stdout=" M metadata/x\n")
        if cmd[:2] == ["git", "push"]:
            push_attempts["n"] += 1
            return _fake_completed(0 if push_attempts["n"] >= 2 else 1, stderr="rejected")
        return _fake_completed(0)

    monkeypatch.setattr(m, "run", fake_run)
    rows = [{"distro": "humble", "package": "p"}]
    m.push_metadata(staged, rows)  # should not raise
    assert push_attempts["n"] == 2


def test_push_metadata_exits_after_max_retries(monkeypatch, tmp_path):
    staged = tmp_path / "staged"
    staged.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)

    def fake_run(cmd, cwd=None, check=True):
        if cmd[:3] == ["git", "worktree", "add"]:
            from pathlib import Path as _P

            _P(cmd[3]).mkdir(parents=True, exist_ok=True)
        if cmd[:2] == ["git", "status"]:
            return _fake_completed(0, stdout=" M metadata/x\n")
        if cmd[:2] == ["git", "push"]:
            return _fake_completed(1, stderr="still rejected")
        return _fake_completed(0)

    monkeypatch.setattr(m, "run", fake_run)
    rows = [{"distro": "humble", "package": "p"}]
    with pytest.raises(SystemExit):
        m.push_metadata(staged, rows)
