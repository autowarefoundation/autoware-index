"""Tests for scripts/cache_package_xml.py (metadata cache backfill).

The script is the MANUAL/BACKFILL path for the metadata/ cache (the sweep
populates it in the normal pipeline). Focus is the pure parts:
  - find_package_xml: picks the package.xml whose <name> matches, not the first.
  - repo_groups_from_distributions: one group per registered REPOSITORY
    (schema_version "2"; clone once, however many packages it hosts).
  - commit_message: pure formatting.

The git/network parts (checkout / cache_group / push_metadata) are exercised
with the side-effecting calls monkeypatched, so no real clones or pushes happen.
"""

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


def make_group(
    distro="humble",
    repo_name="repo_a",
    repository="https://example.com/r.git",
    kind="branch",
    value="main",
    packages=None,
):
    return {
        "distro": distro,
        "repo_name": repo_name,
        "repository": repository,
        "kind": kind,
        "value": value,
        "packages": packages if packages is not None else ["my_pkg"],
    }


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
# repo_groups_from_distributions
# --------------------------------------------------------------------------
def write_distribution(dirpath, filename, content):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / filename).write_text(content)


def test_repo_groups_one_repository(tmp_path):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
schema_version: "2"
ros_distro: humble
repositories:
  repo_a:
    url: https://example.com/repo.git
    ref:
      kind: tag
      value: 1.0.0
    packages:
      my_pkg:
        tags: [sensing]
""",
    )
    groups = m.repo_groups_from_distributions(tmp_path)
    assert groups == [
        {
            "distro": "humble",
            "repo_name": "repo_a",
            "repository": "https://example.com/repo.git",
            "kind": "tag",
            "value": "1.0.0",
            "packages": ["my_pkg"],
        }
    ]


def test_repo_groups_one_group_for_multi_package_repo(tmp_path):
    # ONE group per repository however many packages it hosts; package names
    # come out sorted.
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
schema_version: "2"
ros_distro: humble
repositories:
  shared_repo:
    url: https://example.com/shared.git
    ref:
      kind: branch
      value: main
    packages:
      zeta_pkg:
        tags: [a]
      alpha_pkg:
        tags: [a]
""",
    )
    groups = m.repo_groups_from_distributions(tmp_path)
    assert len(groups) == 1
    assert groups[0]["repo_name"] == "shared_repo"
    assert groups[0]["packages"] == ["alpha_pkg", "zeta_pkg"]


def test_repo_groups_defaults_distro_to_stem(tmp_path):
    # No ros_distro key -> the file stem is used as the distro.
    write_distribution(
        tmp_path,
        "jazzy.yaml",
        """
schema_version: "2"
repositories:
  repo_a:
    url: repo
    ref:
      value: main
    packages:
      pkg:
        tags: [a]
""",
    )
    groups = m.repo_groups_from_distributions(tmp_path)
    assert groups[0]["distro"] == "jazzy"


def test_repo_groups_defaults_kind_to_branch(tmp_path):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
schema_version: "2"
ros_distro: humble
repositories:
  repo_a:
    url: repo
    ref:
      value: somebranch
    packages:
      pkg:
        tags: [a]
""",
    )
    groups = m.repo_groups_from_distributions(tmp_path)
    assert groups[0]["kind"] == "branch"
    assert groups[0]["value"] == "somebranch"


def test_repo_groups_skips_missing_url(tmp_path, capsys):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
schema_version: "2"
ros_distro: humble
repositories:
  no_url_repo:
    ref:
      value: main
    packages:
      pkg_x:
        tags: [a]
  good_repo:
    url: repo
    ref:
      value: main
    packages:
      pkg_y:
        tags: [a]
""",
    )
    groups = m.repo_groups_from_distributions(tmp_path)
    assert [g["repo_name"] for g in groups] == ["good_repo"]
    err = capsys.readouterr().err
    assert "no_url_repo" in err
    assert "missing url/ref/packages" in err
    assert "::warning::" in err


def test_repo_groups_skips_missing_ref_value(tmp_path, capsys):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
schema_version: "2"
ros_distro: humble
repositories:
  no_value_repo:
    url: repo
    packages:
      pkg:
        tags: [a]
""",
    )
    assert m.repo_groups_from_distributions(tmp_path) == []
    assert "no_value_repo" in capsys.readouterr().err


def test_repo_groups_skips_empty_packages(tmp_path, capsys):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
schema_version: "2"
ros_distro: humble
repositories:
  no_packages_repo:
    url: repo
    ref:
      value: main
    packages: {}
""",
    )
    assert m.repo_groups_from_distributions(tmp_path) == []
    assert "no_packages_repo" in capsys.readouterr().err


def test_repo_groups_null_repo_spec_skipped(tmp_path, capsys):
    # A repo entry mapped to null (spec is None) must not crash; warn + skip.
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
schema_version: "2"
ros_distro: humble
repositories:
  null_repo:
""",
    )
    assert m.repo_groups_from_distributions(tmp_path) == []
    assert "null_repo" in capsys.readouterr().err


def test_repo_groups_multiple_files_and_repos_sorted(tmp_path):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
schema_version: "2"
ros_distro: humble
repositories:
  zzz_repo:
    url: rz
    ref:
      value: vz
    packages:
      z_pkg:
        tags: [a]
  aaa_repo:
    url: ra
    ref:
      value: va
    packages:
      a_pkg:
        tags: [a]
""",
    )
    write_distribution(
        tmp_path,
        "jazzy.yaml",
        """
schema_version: "2"
ros_distro: jazzy
repositories:
  mid_repo:
    url: rm
    ref:
      value: vm
    packages:
      m_pkg:
        tags: [a]
""",
    )
    groups = m.repo_groups_from_distributions(tmp_path)
    # files iterated sorted by name; repos sorted by entry name within a file.
    assert [(g["distro"], g["repo_name"]) for g in groups] == [
        ("humble", "aaa_repo"),
        ("humble", "zzz_repo"),
        ("jazzy", "mid_repo"),
    ]


def test_repo_groups_empty_dir(tmp_path):
    assert m.repo_groups_from_distributions(tmp_path) == []


def test_repo_groups_ignores_non_yaml(tmp_path):
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
schema_version: "2"
ros_distro: humble
repositories:
  repo_a:
    url: ra
    ref:
      value: va
    packages:
      a:
        tags: [x]
""",
    )
    (tmp_path / "README.md").write_text("not a distribution")
    (tmp_path / "notes.txt").write_text("ignore me")
    groups = m.repo_groups_from_distributions(tmp_path)
    assert [g["repo_name"] for g in groups] == ["repo_a"]


def test_repo_groups_v1_file_exits(tmp_path):
    # schema_version "1" hard-fails through the shared loader -> sys.exit.
    write_distribution(
        tmp_path,
        "humble.yaml",
        """
schema_version: "1"
ros_distro: humble
packages:
  my_pkg:
    repository: https://example.com/repo.git
    ref:
      kind: tag
      value: 1.0.0
""",
    )
    with pytest.raises(SystemExit) as excinfo:
        m.repo_groups_from_distributions(tmp_path)
    assert str(excinfo.value).startswith("::error::")
    assert "not supported" in str(excinfo.value)


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
# cache_group (checkout monkeypatched; real find_package_xml + copy)
# --------------------------------------------------------------------------
def test_cache_group_one_checkout_for_many_packages(monkeypatch, tmp_path):
    # The whole point of grouping: ONE clone per repository, N files cached.
    checkouts = []

    def fake_checkout(repository, kind, value, dest):
        checkouts.append((repository, kind, value))
        write_package_xml(dest / "a", "alpha", description="ALPHA")
        write_package_xml(dest / "b", "beta", description="BETA")
        return True

    monkeypatch.setattr(m, "checkout", fake_checkout)
    out = tmp_path / "out"
    group = make_group(distro="humble", packages=["alpha", "beta"])
    rows = m.cache_group(group, out)

    assert len(checkouts) == 1
    assert rows == [
        {"distro": "humble", "package": "alpha"},
        {"distro": "humble", "package": "beta"},
    ]
    assert "ALPHA" in (out / "humble" / "alpha.xml").read_text()
    assert "BETA" in (out / "humble" / "beta.xml").read_text()


def test_cache_group_single_package(monkeypatch, tmp_path):
    def fake_checkout(repository, kind, value, dest):
        write_package_xml(dest / "src" / "my_pkg", "my_pkg", description="Hello")
        return True

    monkeypatch.setattr(m, "checkout", fake_checkout)
    out = tmp_path / "out"
    rows = m.cache_group(make_group(packages=["my_pkg"]), out)
    assert rows == [{"distro": "humble", "package": "my_pkg"}]

    cached = out / "humble" / "my_pkg.xml"
    assert cached.exists()
    assert "Hello" in cached.read_text()


def test_cache_group_checkout_failure_returns_no_rows(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(m, "checkout", lambda *a, **k: False)
    out = tmp_path / "out"
    rows = m.cache_group(make_group(packages=["p"]), out)
    assert rows == []
    err = capsys.readouterr().err
    assert "could not check out" in err
    assert not (out / "humble" / "p.xml").exists()


def test_cache_group_missing_package_xml_errors_but_others_cache(monkeypatch, tmp_path, capsys):
    # One registered package has no matching package.xml in the tree: it gets
    # a ::error, while its siblings still cache (no all-or-nothing).
    def fake_checkout(repository, kind, value, dest):
        write_package_xml(dest / "a", "alpha", description="ALPHA")
        return True

    monkeypatch.setattr(m, "checkout", fake_checkout)
    out = tmp_path / "out"
    rows = m.cache_group(make_group(packages=["alpha", "ghost_pkg"]), out)

    assert rows == [{"distro": "humble", "package": "alpha"}]
    assert (out / "humble" / "alpha.xml").exists()
    assert not (out / "humble" / "ghost_pkg.xml").exists()
    err = capsys.readouterr().err
    assert "no package.xml" in err
    assert "ghost_pkg" in err
    assert "::error::" in err


def test_cache_group_picks_matching_among_many(monkeypatch, tmp_path):
    def fake_checkout(repository, kind, value, dest):
        write_package_xml(dest / "a", "alpha", description="ALPHA")
        write_package_xml(dest / "b", "beta", description="BETA")
        return True

    monkeypatch.setattr(m, "checkout", fake_checkout)
    out = tmp_path / "out"
    rows = m.cache_group(make_group(packages=["beta"]), out)
    assert rows == [{"distro": "humble", "package": "beta"}]
    assert "BETA" in (out / "humble" / "beta.xml").read_text()


def test_cache_group_cleans_up_tempdir(monkeypatch, tmp_path):
    captured = {}

    def fake_checkout(repository, kind, value, dest):
        captured["dest"] = dest
        write_package_xml(dest / "p", "p")
        return True

    monkeypatch.setattr(m, "checkout", fake_checkout)
    out = tmp_path / "out"
    assert m.cache_group(make_group(packages=["p"]), out) != []
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


def test_push_metadata_merges_into_existing_tree(monkeypatch, tmp_path):
    # Regression: a partial sweep stages only the swept packages' files, so
    # push_metadata must MERGE into the worktree's existing metadata/ tree.
    # The old rmtree+copytree replaced the whole tree, deleting every
    # non-swept package's cached package.xml on every partial sweep.
    staged = tmp_path / "staged"
    (staged / "jazzy").mkdir(parents=True)
    (staged / "jazzy" / "swept.xml").write_text("<package><name>swept</name></package>")

    monkeypatch.chdir(tmp_path)
    worktree = {}

    def fake_run(cmd, cwd=None, check=True):
        if cmd[:3] == ["git", "worktree", "add"]:
            from pathlib import Path as _P

            wt = _P(cmd[3])
            # Pre-existing cache: a non-swept sibling in the same distro, a
            # package in another distro, and a stale copy of the swept file.
            (wt / "metadata" / "jazzy").mkdir(parents=True, exist_ok=True)
            (wt / "metadata" / "jazzy" / "other.xml").write_text("<package><name>other</name></package>")
            (wt / "metadata" / "jazzy" / "swept.xml").write_text("STALE")
            (wt / "metadata" / "humble").mkdir(parents=True, exist_ok=True)
            (wt / "metadata" / "humble" / "third.xml").write_text("<package><name>third</name></package>")
            worktree["path"] = wt
        if cmd[:2] == ["git", "status"]:
            return _fake_completed(0, stdout=" M metadata/jazzy/swept.xml\n")
        return _fake_completed(0)

    monkeypatch.setattr(m, "run", fake_run)
    m.push_metadata(staged, [{"distro": "jazzy", "package": "swept"}])

    wt = worktree["path"]
    # Swept file refreshed, everything not in this sweep left untouched.
    assert (wt / "metadata" / "jazzy" / "swept.xml").read_text() == "<package><name>swept</name></package>"
    assert (wt / "metadata" / "jazzy" / "other.xml").exists()
    assert (wt / "metadata" / "humble" / "third.xml").exists()


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


# --------------------------------------------------------------------------
# main (CLI surface: --distributions-dir is the one required input)
# --------------------------------------------------------------------------
def test_main_requires_distributions_dir(monkeypatch):
    # --matrix-file is gone; --distributions-dir is required, not part of a
    # mutually-exclusive group. argparse exits with usage error code 2.
    monkeypatch.setattr("sys.argv", ["cache_package_xml.py"])
    with pytest.raises(SystemExit) as excinfo:
        m.main()
    assert excinfo.value.code == 2


def test_main_no_repositories_early_return(monkeypatch, tmp_path, capsys):
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        ["cache_package_xml.py", "--distributions-dir", str(dist), "--out", str(tmp_path / "out")],
    )
    m.main()
    assert "no repositories to cache" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()  # returned before mkdir


def test_main_caches_locally_without_push(monkeypatch, tmp_path, capsys):
    write_distribution(
        tmp_path / "dist",
        "humble.yaml",
        """
schema_version: "2"
ros_distro: humble
repositories:
  repo_a:
    url: https://example.com/repo.git
    ref:
      kind: branch
      value: main
    packages:
      my_pkg:
        tags: [sensing]
""",
    )

    def fake_checkout(repository, kind, value, dest):
        write_package_xml(dest / "my_pkg", "my_pkg", description="Hello")
        return True

    monkeypatch.setattr(m, "checkout", fake_checkout)
    monkeypatch.setattr(
        m, "push_metadata",
        lambda *a, **k: pytest.fail("push_metadata must not run without --push"),
    )
    out = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv",
        ["cache_package_xml.py", "--distributions-dir", str(tmp_path / "dist"), "--out", str(out)],
    )
    m.main()

    assert (out / "humble" / "my_pkg.xml").exists()
    err = capsys.readouterr().err
    assert "cached 1/1 package.xml file(s) from 1 repository clone(s)" in err
