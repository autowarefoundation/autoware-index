"""Tests for scripts/check_refs.py — the semantic validation guard (H2).

Focus: placeholder-maintainer rejection (repo-level AND per-package override),
sha format validation, canonical-URL duplicate detection, per-distro
package-name uniqueness, offline behavior (no network when network=False),
ls-remote memoization through the shared resolve_cache, and the check_file
integration over a real temp distributions yaml (schema_version "2").
ls_remote is exercised by monkeypatching subprocess so no real network call is
ever made.
"""

import textwrap
from types import SimpleNamespace

import pytest

import check_refs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

REAL_MAINTAINER = {
    "name": "Ryohsuke Mitsudome",
    "email": "ryohsuke.mitsudome@tier4.jp",
    "github": "mitsudome-r",
}

GOOD_SHA = "a" * 40  # 40 lowercase hex chars


def write_yaml(tmp_path, body, filename="jazzy.yaml"):
    p = tmp_path / filename
    p.write_text(textwrap.dedent(body))
    return p


# ===========================================================================
# check_maintainers
# ===========================================================================

class TestCheckMaintainers:
    def test_real_maintainer_accepted(self):
        assert check_refs.check_maintainers("f.yaml", "repo", [REAL_MAINTAINER]) == []

    def test_multiple_real_maintainers_accepted(self):
        second = {
            "name": "Kenzo Lobos-Tsunekawa",
            "email": "kenzo.lobos@tier4.jp",
            "github": "knzo25",
        }
        assert check_refs.check_maintainers("f.yaml", "repo", [REAL_MAINTAINER, second]) == []

    def test_empty_list_accepted(self):
        assert check_refs.check_maintainers("f.yaml", "repo", []) == []

    def test_none_list_accepted(self):
        # `maintainers or []` guards a None argument.
        assert check_refs.check_maintainers("f.yaml", "repo", None) == []

    # --- placeholder NAME signals --------------------------------------
    @pytest.mark.parametrize("placeholder", ["TBD", "tbd", "ToDo", "N/A", "na", "None", "XXX"])
    def test_placeholder_name_rejected(self, placeholder):
        m = {"name": placeholder, "email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        assert len(errors) == 1
        assert "placeholder maintainer name" in errors[0]
        assert placeholder in errors[0]

    def test_name_check_is_case_insensitive(self):
        m = {"name": "Tbd", "email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        assert any("placeholder maintainer name" in e for e in errors)

    def test_missing_name_is_treated_as_placeholder(self):
        # name defaults to "" which is in PLACEHOLDER_NAMES.
        m = {"email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        assert any("placeholder maintainer name" in e for e in errors)

    def test_whitespace_only_name_is_placeholder(self):
        # name is .strip()'d, so "   " collapses to "" -> placeholder.
        m = {"name": "   ", "email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        assert any("placeholder maintainer name" in e for e in errors)

    # --- placeholder GITHUB signals ------------------------------------
    @pytest.mark.parametrize("placeholder", ["TBD", "tbd", "todo", "none", "xxx"])
    def test_placeholder_github_rejected(self, placeholder):
        m = {"name": "Real Name", "email": "real@tier4.jp", "github": placeholder}
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        assert any("placeholder maintainer github" in e for e in errors)
        assert any(placeholder in e for e in errors)

    def test_missing_github_is_treated_as_placeholder(self):
        # github defaults to "" -> placeholder.
        m = {"name": "Real Name", "email": "real@tier4.jp"}
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        assert any("placeholder maintainer github" in e for e in errors)

    # --- placeholder EMAIL signals -------------------------------------
    @pytest.mark.parametrize("email", ["tbd@example.com", "person@example.com", "x@example.org"])
    def test_placeholder_email_rejected(self, email):
        m = {"name": "Real Name", "email": email, "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        assert any("placeholder maintainer email" in e for e in errors)

    def test_email_check_is_case_insensitive(self):
        m = {"name": "Real Name", "email": "Person@Example.COM", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        # email is lowercased before the suffix check.
        assert any("placeholder maintainer email" in e for e in errors)
        assert any("person@example.com" in e for e in errors)

    def test_real_email_not_flagged(self):
        m = {"name": "Real Name", "email": "real@tier4.jp", "github": "realuser"}
        # only the example.* domains are placeholders; tier4.jp is real.
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        assert errors == []

    def test_email_substring_example_not_at_end_is_ok(self):
        # endswith, not "contains": example.com.evil.io must not match.
        m = {"name": "Real Name", "email": "a@example.com.tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        assert errors == []

    # --- multiple signals at once --------------------------------------
    def test_all_three_signals_fire_together(self):
        m = {"name": "TBD", "email": "tbd@example.com", "github": "tbd"}
        errors = check_refs.check_maintainers("f.yaml", "repo", [m])
        assert any("name" in e for e in errors)
        assert any("github" in e for e in errors)
        assert any("email" in e for e in errors)
        assert len(errors) == 3

    def test_error_message_contains_file_and_owner(self):
        # the owner label is whatever the caller passes (repo entry name, or
        # "repo.package" for a per-package override) — opaque here.
        m = {"name": "TBD", "email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("distributions/jazzy.yaml", "my_repo", [m])
        assert errors[0].startswith("distributions/jazzy.yaml::my_repo:")

    def test_one_bad_maintainer_among_good_ones(self):
        bad = {"name": "TBD", "email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "repo", [REAL_MAINTAINER, bad])
        assert len(errors) == 1
        assert "placeholder maintainer name" in errors[0]


# ===========================================================================
# check_ref — sha kind
# ===========================================================================

class TestCheckRefSha:
    def test_valid_sha_no_network(self):
        ref = {"kind": "sha", "value": GOOD_SHA}
        assert check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=False, resolve_cache={}) == []

    def test_valid_sha_with_network_does_not_probe(self, monkeypatch):
        # sha kind returns before any ls_remote call even when network=True.
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote must not be called for sha"),
        )
        ref = {"kind": "sha", "value": GOOD_SHA}
        assert check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=True, resolve_cache={}) == []

    def test_short_sha_rejected(self):
        ref = {"kind": "sha", "value": "abc123"}
        errors = check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_uppercase_sha_rejected(self):
        ref = {"kind": "sha", "value": "A" * 40}
        errors = check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=False, resolve_cache={})
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_non_hex_sha_rejected(self):
        ref = {"kind": "sha", "value": "g" * 40}
        errors = check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=False, resolve_cache={})
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_too_long_sha_rejected(self):
        ref = {"kind": "sha", "value": "a" * 41}
        errors = check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=False, resolve_cache={})
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_missing_value_sha_rejected(self):
        # value defaults to "" -> not a valid sha.
        ref = {"kind": "sha"}
        errors = check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=False, resolve_cache={})
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_sha_error_names_file_and_repo_entry(self):
        ref = {"kind": "sha", "value": "nope"}
        errors = check_refs.check_ref(
            "distributions/jazzy.yaml", "my_repo", "https://r", ref, network=False, resolve_cache={}
        )
        assert errors[0].startswith("distributions/jazzy.yaml::my_repo:")


# ===========================================================================
# check_ref — branch/tag kinds and the network gate
# ===========================================================================

class TestCheckRefNetworkGate:
    def test_branch_no_network_skipped(self, monkeypatch):
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote must not run with network=False"),
        )
        ref = {"kind": "branch", "value": "main"}
        assert check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=False, resolve_cache={}) == []

    def test_tag_no_network_skipped(self, monkeypatch):
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote must not run with network=False"),
        )
        ref = {"kind": "tag", "value": "1.0.0"}
        assert check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=False, resolve_cache={}) == []

    def test_unknown_kind_no_network_skipped(self):
        ref = {"kind": "nonsense", "value": "x"}
        assert check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=False, resolve_cache={}) == []

    def test_unknown_kind_with_network_probes_as_tag(self, monkeypatch):
        # kind is neither sha nor branch: the source falls through to the
        # --tags ls-remote probe (the JSON schema keeps kinds valid upstream;
        # this documents the REAL fallthrough behavior, not a contract).
        calls = []

        def fake_ls_remote(repository, ref_filter, value):
            calls.append((repository, ref_filter, value))
            return True

        monkeypatch.setattr(check_refs, "ls_remote", fake_ls_remote)
        ref = {"kind": "nonsense", "value": "x"}
        assert check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=True, resolve_cache={}) == []
        assert calls == [("https://r", "--tags", "x")]

    def test_missing_kind_with_network_probes_as_tag(self, monkeypatch):
        # kind=None is cached under "" and probed with --tags, same fallthrough.
        calls = []
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda repository, ref_filter, value: calls.append((repository, ref_filter, value)) or True,
        )
        ref = {"value": "main"}
        cache = {}
        assert check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=True, resolve_cache=cache) == []
        assert calls == [("https://r", "--tags", "main")]
        assert ("https://r", "", "main") in cache


class TestCheckRefBranchTagNetwork:
    def test_branch_resolves(self, monkeypatch):
        calls = []

        def fake_ls_remote(repository, ref_filter, value):
            calls.append((repository, ref_filter, value))
            return True

        monkeypatch.setattr(check_refs, "ls_remote", fake_ls_remote)
        ref = {"kind": "branch", "value": "main"}
        assert check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=True, resolve_cache={}) == []
        assert calls == [("https://r", "--heads", "main")]

    def test_branch_does_not_resolve(self, monkeypatch):
        monkeypatch.setattr(check_refs, "ls_remote", lambda *a, **k: False)
        ref = {"kind": "branch", "value": "ghost"}
        errors = check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=True, resolve_cache={})
        assert len(errors) == 1
        assert "branch ref 'ghost' does not resolve in https://r" in errors[0]
        assert "git ls-remote found no match" in errors[0]

    def test_tag_resolves(self, monkeypatch):
        calls = []

        def fake_ls_remote(repository, ref_filter, value):
            calls.append((repository, ref_filter, value))
            return True

        monkeypatch.setattr(check_refs, "ls_remote", fake_ls_remote)
        ref = {"kind": "tag", "value": "1.0.0"}
        assert check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=True, resolve_cache={}) == []
        assert calls == [("https://r", "--tags", "1.0.0")]

    def test_tag_does_not_resolve(self, monkeypatch):
        monkeypatch.setattr(check_refs, "ls_remote", lambda *a, **k: False)
        ref = {"kind": "tag", "value": "0.2.1"}
        errors = check_refs.check_ref("f.yaml", "repo", "https://r", ref, network=True, resolve_cache={})
        assert len(errors) == 1
        assert "tag ref '0.2.1' does not resolve in https://r" in errors[0]


# ===========================================================================
# check_ref — ls-remote memoization through resolve_cache
# ===========================================================================

class TestCheckRefMemoization:
    def _counting_ls_remote(self, monkeypatch, result=True):
        calls = []

        def fake_ls_remote(repository, ref_filter, value):
            calls.append((repository, ref_filter, value))
            return result

        monkeypatch.setattr(check_refs, "ls_remote", fake_ls_remote)
        return calls

    def test_same_key_probed_once(self, monkeypatch):
        calls = self._counting_ls_remote(monkeypatch)
        cache = {}
        ref = {"kind": "branch", "value": "main"}
        assert check_refs.check_ref("a.yaml", "r1", "https://r", ref, network=True, resolve_cache=cache) == []
        assert check_refs.check_ref("b.yaml", "r2", "https://r", ref, network=True, resolve_cache=cache) == []
        assert len(calls) == 1
        # cache is keyed on (url, kind, value) — independent of file/entry.
        assert cache == {("https://r", "branch", "main"): True}

    def test_negative_result_cached_but_still_errors(self, monkeypatch):
        calls = self._counting_ls_remote(monkeypatch, result=False)
        cache = {}
        ref = {"kind": "tag", "value": "v9"}
        e1 = check_refs.check_ref("a.yaml", "r1", "https://r", ref, network=True, resolve_cache=cache)
        e2 = check_refs.check_ref("b.yaml", "r2", "https://r", ref, network=True, resolve_cache=cache)
        # both call sites report the failure, but the probe ran only once.
        assert len(e1) == 1 and len(e2) == 1
        assert len(calls) == 1

    def test_distinct_keys_probe_separately(self, monkeypatch):
        calls = self._counting_ls_remote(monkeypatch)
        cache = {}
        check_refs.check_ref("f.yaml", "r", "https://r", {"kind": "branch", "value": "main"}, True, cache)
        check_refs.check_ref("f.yaml", "r", "https://r", {"kind": "tag", "value": "main"}, True, cache)
        check_refs.check_ref("f.yaml", "r", "https://other", {"kind": "branch", "value": "main"}, True, cache)
        assert len(calls) == 3


# ===========================================================================
# ls_remote — monkeypatch subprocess.run; never hit the network
# ===========================================================================

class TestLsRemote:
    def test_resolves_when_stdout_nonempty(self, monkeypatch):
        captured = {}

        def fake_run(cmd, capture_output, text):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")

        monkeypatch.setattr(check_refs.subprocess, "run", fake_run)
        assert check_refs.ls_remote("https://r", "--heads", "main") is True
        assert captured["cmd"] == ["git", "ls-remote", "--heads", "https://r", "main"]

    def test_not_resolved_when_stdout_empty(self, monkeypatch):
        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=0, stdout="   \n", stderr="")

        monkeypatch.setattr(check_refs.subprocess, "run", fake_run)
        assert check_refs.ls_remote("https://r", "--tags", "nope") is False

    def test_not_resolved_on_nonzero_returncode(self, monkeypatch):
        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=128, stdout="anything\n", stderr="fatal")

        monkeypatch.setattr(check_refs.subprocess, "run", fake_run)
        assert check_refs.ls_remote("https://r", "--heads", "main") is False

    def test_ls_remote_against_real_local_git_repo(self, git_repo):
        # Exercise the REAL subprocess path against a local file:// repo so we
        # confirm the command shape works without touching the network.
        (git_repo.path / "f.txt").write_text("x")
        git_repo.commit("init")
        git_repo.git("tag", "v1.0.0")
        url = git_repo.path.as_uri()  # file:// — local, no network

        assert check_refs.ls_remote(url, "--heads", "main") is True
        assert check_refs.ls_remote(url, "--tags", "v1.0.0") is True
        assert check_refs.ls_remote(url, "--heads", "does-not-exist") is False
        assert check_refs.ls_remote(url, "--tags", "v9.9.9") is False


# ===========================================================================
# check_file — integration over a temp distributions yaml (schema_version 2)
# ===========================================================================

class TestCheckFile:
    def _clean_body(self):
        return """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              autoware_livox_tag_filter:
                url: https://github.com/autowarefoundation/autoware_livox_tag_filter
                maintainers:
                  - name: Ryohsuke Mitsudome
                    email: ryohsuke.mitsudome@tier4.jp
                    github: mitsudome-r
                ref:
                  kind: sha
                  value: "%s"
                packages:
                  autoware_livox_tag_filter:
                    tags: [sensing]
            """ % GOOD_SHA

    def test_clean_file_no_errors_offline(self, tmp_path):
        p = write_yaml(tmp_path, self._clean_body())
        assert check_refs.check_file(p, network=False, resolve_cache={}) == []

    def test_clean_file_no_errors_with_network_sha_only(self, tmp_path, monkeypatch):
        # sha ref needs no ls_remote; network=True must still pass without one.
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote should not run for a sha ref"),
        )
        p = write_yaml(tmp_path, self._clean_body())
        assert check_refs.check_file(p, network=True, resolve_cache={}) == []

    def test_repo_level_placeholder_maintainer_flagged(self, tmp_path):
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              repo_a:
                url: https://github.com/x/y
                maintainers:
                  - name: TBD
                    email: tbd@example.com
                    github: tbd
                ref:
                  kind: sha
                  value: "%s"
                packages:
                  pkg_a:
                    tags: [sensing]
            """ % GOOD_SHA
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        # three placeholder signals from the single bad maintainer, owned by
        # the repo entry (default maintainers).
        assert len(errors) == 3
        assert all("::repo_a:" in e for e in errors)

    def test_per_package_override_maintainer_flagged(self, tmp_path):
        # a clean repo-level default plus a placeholder per-package OVERRIDE:
        # the override must be checked too, owned as "repo.package".
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              repo_a:
                url: https://github.com/x/y
                maintainers:
                  - name: Ryohsuke Mitsudome
                    email: ryohsuke.mitsudome@tier4.jp
                    github: mitsudome-r
                ref:
                  kind: sha
                  value: "%s"
                packages:
                  pkg_a:
                    tags: [sensing]
                    maintainers:
                      - name: TBD
                        email: real@tier4.jp
                        github: realuser
            """ % GOOD_SHA
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "::repo_a.pkg_a:" in errors[0]
        assert "placeholder maintainer name" in errors[0]

    def test_repo_and_package_maintainers_both_checked(self, tmp_path):
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              repo_a:
                url: https://github.com/x/y
                maintainers:
                  - name: TBD
                    email: real@tier4.jp
                    github: realuser
                ref:
                  kind: sha
                  value: "%s"
                packages:
                  pkg_a:
                    tags: [sensing]
                    maintainers:
                      - name: Real Name
                        email: real@tier4.jp
                        github: xxx
            """ % GOOD_SHA
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 2
        assert any("::repo_a:" in e and "name" in e for e in errors)
        assert any("::repo_a.pkg_a:" in e and "github" in e for e in errors)

    def test_bad_ref_flagged(self, tmp_path):
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              repo_a:
                url: https://github.com/x/y
                ref:
                  kind: sha
                  value: "not-a-sha"
                packages:
                  pkg_a:
                    tags: [sensing]
            """
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_branch_ref_not_resolving_with_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(check_refs, "ls_remote", lambda *a, **k: False)
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              repo_a:
                url: https://github.com/x/y
                ref:
                  kind: branch
                  value: ghost-branch
                packages:
                  pkg_a:
                    tags: [sensing]
            """
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=True, resolve_cache={})
        assert len(errors) == 1
        assert "branch ref 'ghost-branch' does not resolve" in errors[0]

    def test_ref_without_url_is_not_checked(self, tmp_path, monkeypatch):
        # check_file only calls check_ref when BOTH ref and url are truthy.
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote should not run without a url"),
        )
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              repo_a:
                ref:
                  kind: branch
                  value: main
                packages:
                  pkg_a:
                    tags: [sensing]
            """
        p = write_yaml(tmp_path, body)
        assert check_refs.check_file(p, network=True, resolve_cache={}) == []

    # --- duplicate-URL detection ----------------------------------------
    def test_duplicate_url_spelling_variants_rejected(self, tmp_path):
        # https + .git vs ssh shorthand vs case: same canonical repo.
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              first_entry:
                url: https://github.com/Org/Repo.git
                packages:
                  pkg_one:
                    tags: [sensing]
              second_entry:
                url: git@github.com:org/repo
                packages:
                  pkg_two:
                    tags: [sensing]
            """
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        # the error names the colliding entry AND the entry it collides with.
        assert "::second_entry:" in errors[0]
        assert "'first_entry'" in errors[0]
        assert "github.com/org/repo" in errors[0]  # the canonical form
        assert "one entry per repository" in errors[0]

    def test_duplicate_url_trailing_slash_and_scheme_case(self, tmp_path):
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              entry_a:
                url: https://github.com/org/repo/
                packages:
                  pkg_one:
                    tags: [sensing]
              entry_b:
                url: HTTPS://github.com/ORG/REPO.git
                packages:
                  pkg_two:
                    tags: [sensing]
            """
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "repository URL duplicates entry" in errors[0]

    def test_distinct_urls_not_flagged(self, tmp_path):
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              entry_a:
                url: https://github.com/org/alpha
                packages:
                  pkg_one:
                    tags: [sensing]
              entry_b:
                url: https://github.com/org/beta
                packages:
                  pkg_two:
                    tags: [sensing]
            """
        p = write_yaml(tmp_path, body)
        assert check_refs.check_file(p, network=False, resolve_cache={}) == []

    # --- duplicate-package detection --------------------------------------
    def test_duplicate_package_across_repo_entries_rejected(self, tmp_path):
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              alpha_repo:
                url: https://github.com/x/alpha
                packages:
                  shared_pkg:
                    tags: [sensing]
              beta_repo:
                url: https://github.com/x/beta
                packages:
                  shared_pkg:
                    tags: [sensing]
            """
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        # names BOTH entries: the offender in the prefix, the original quoted.
        assert "::beta_repo:" in errors[0]
        assert "'alpha_repo'" in errors[0]
        assert "'shared_pkg'" in errors[0]
        assert "unique per distro" in errors[0]

    def test_distinct_packages_across_repo_entries_ok(self, tmp_path):
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              alpha_repo:
                url: https://github.com/x/alpha
                packages:
                  pkg_one:
                    tags: [sensing]
              beta_repo:
                url: https://github.com/x/beta
                packages:
                  pkg_two:
                    tags: [sensing]
            """
        p = write_yaml(tmp_path, body)
        assert check_refs.check_file(p, network=False, resolve_cache={}) == []

    # --- ls-remote memoization across files --------------------------------
    def test_resolve_cache_shared_across_files(self, tmp_path, monkeypatch):
        # The same (url, kind, value) registered in two distro files must
        # resolve with exactly ONE ls-remote probe via the shared cache.
        calls = []

        def fake_ls_remote(repository, ref_filter, value):
            calls.append((repository, ref_filter, value))
            return True

        monkeypatch.setattr(check_refs, "ls_remote", fake_ls_remote)
        body = """\
            schema_version: "2"
            ros_distro: %s
            repositories:
              shared_repo:
                url: https://github.com/x/y
                ref:
                  kind: branch
                  value: main
                packages:
                  pkg_%s:
                    tags: [sensing]
            """
        p1 = write_yaml(tmp_path, body % ("jazzy", "j"), filename="jazzy.yaml")
        p2 = write_yaml(tmp_path, body % ("humble", "h"), filename="humble.yaml")

        cache = {}
        assert check_refs.check_file(p1, network=True, resolve_cache=cache) == []
        assert check_refs.check_file(p2, network=True, resolve_cache=cache) == []
        assert calls == [("https://github.com/x/y", "--heads", "main")]

    # --- loader-gate failures -----------------------------------------------
    def test_schema_version_1_is_a_check_failure(self, tmp_path):
        # v1 flat-packages files now hard-fail through the shared loader.
        body = """\
            schema_version: "1"
            ros_distro: jazzy
            packages:
              pkg_a:
                repository: https://github.com/x/y
            """
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "not supported" in errors[0]

    def test_missing_schema_version_is_a_check_failure(self, tmp_path):
        # No silent default: a file without schema_version errors loudly.
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories: {}
            """
        # sanity: an explicit "2" with empty repositories is clean...
        p = write_yaml(tmp_path, body)
        assert check_refs.check_file(p, network=False, resolve_cache={}) == []
        # ...while omitting schema_version errors.
        p2 = write_yaml(tmp_path, "ros_distro: jazzy\nrepositories: {}\n", filename="humble.yaml")
        errors = check_refs.check_file(p2, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "not supported" in errors[0]

    def test_empty_file_errors(self, tmp_path):
        # An empty doc is no longer a silent pass — the loader rejects it.
        p = tmp_path / "empty.yaml"
        p.write_text("")
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "expected a YAML mapping" in errors[0]

    def test_top_level_not_a_dict_errors(self, tmp_path):
        p = write_yaml(tmp_path, "- a\n- b\n")
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "expected a YAML mapping" in errors[0]

    def test_missing_repositories_key_errors(self, tmp_path):
        p = write_yaml(tmp_path, "schema_version: \"2\"\nros_distro: jazzy\n")
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "`repositories` must be a mapping" in errors[0]

    def test_yaml_parse_error_reported(self, tmp_path):
        p = tmp_path / "broken.yaml"
        p.write_text("repositories: [unclosed\n")
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "cannot parse" in errors[0]

    def test_null_repo_spec_handled(self, tmp_path):
        # `spec or {}` guards a repo entry mapped to null.
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              repo_a:
            """
        p = write_yaml(tmp_path, body)
        assert check_refs.check_file(p, network=False, resolve_cache={}) == []

    def test_multiple_repos_accumulate_errors(self, tmp_path):
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              good_repo:
                url: https://github.com/x/y
                maintainers:
                  - name: Real Name
                    email: real@tier4.jp
                    github: realuser
                ref:
                  kind: sha
                  value: "%s"
                packages:
                  good_pkg:
                    tags: [sensing]
              bad_repo:
                url: https://github.com/x/z
                maintainers:
                  - name: TBD
                    email: real@tier4.jp
                    github: realuser
                ref:
                  kind: sha
                  value: "%s"
                packages:
                  bad_pkg:
                    tags: [sensing]
            """ % (GOOD_SHA, GOOD_SHA)
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False, resolve_cache={})
        assert len(errors) == 1
        assert "bad_repo" in errors[0]
        assert all("good_repo" not in e for e in errors)


# ===========================================================================
# main — exit codes + the process-wide shared resolve_cache
# ===========================================================================

class TestMain:
    def test_unsupported_schema_version_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        body = """\
            schema_version: "1"
            ros_distro: jazzy
            packages:
              pkg_a:
                repository: https://github.com/x/y
            """
        p = write_yaml(tmp_path, body)
        monkeypatch.setattr("sys.argv", ["check_refs.py", str(p)])
        assert check_refs.main() == 1
        err = capsys.readouterr().err
        assert "not supported" in err
        assert "1 problem(s) found" in err

    def test_clean_files_exit_zero_and_share_one_cache(self, tmp_path, monkeypatch, capsys):
        # main builds ONE resolve_cache for all paths: the same ref in two
        # files costs a single ls-remote.
        calls = []

        def fake_ls_remote(repository, ref_filter, value):
            calls.append((repository, ref_filter, value))
            return True

        monkeypatch.setattr(check_refs, "ls_remote", fake_ls_remote)
        body = """\
            schema_version: "2"
            ros_distro: %s
            repositories:
              shared_repo:
                url: https://github.com/x/y
                ref:
                  kind: branch
                  value: main
                packages:
                  pkg_a:
                    tags: [sensing]
            """
        p1 = write_yaml(tmp_path, body % "jazzy", filename="jazzy.yaml")
        p2 = write_yaml(tmp_path, body % "humble", filename="humble.yaml")
        monkeypatch.setattr("sys.argv", ["check_refs.py", str(p1), str(p2)])

        assert check_refs.main() == 0
        assert calls == [("https://github.com/x/y", "--heads", "main")]
        assert "no placeholder maintainers" in capsys.readouterr().err

    def test_no_network_flag_skips_ls_remote(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote must not run with --no-network"),
        )
        body = """\
            schema_version: "2"
            ros_distro: jazzy
            repositories:
              repo_a:
                url: https://github.com/x/y
                ref:
                  kind: branch
                  value: main
                packages:
                  pkg_a:
                    tags: [sensing]
            """
        p = write_yaml(tmp_path, body)
        monkeypatch.setattr("sys.argv", ["check_refs.py", "--no-network", str(p)])
        assert check_refs.main() == 0


# ===========================================================================
# module-level constants sanity
# ===========================================================================

class TestConstants:
    def test_placeholder_names_set(self):
        assert "tbd" in check_refs.PLACEHOLDER_NAMES
        assert "" in check_refs.PLACEHOLDER_NAMES

    def test_sha_re_matches_40_lower_hex(self):
        assert check_refs.SHA_RE.match("a1b2c3d4e5f6" + "0" * 28)
        assert not check_refs.SHA_RE.match("A" * 40)
        assert not check_refs.SHA_RE.match("a" * 39)
