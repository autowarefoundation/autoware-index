"""Tests for scripts/check_refs.py — the semantic validation guard (H2).

Focus: placeholder-maintainer rejection, ref-kind validation, offline behavior
(no network when network=False), and the check_file integration over a real
temp distributions yaml. ls_remote is exercised by monkeypatching subprocess so
no real network call is ever made.
"""

import subprocess
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


def write_yaml(tmp_path, body):
    p = tmp_path / "jazzy.yaml"
    p.write_text(textwrap.dedent(body))
    return p


# ===========================================================================
# check_maintainers
# ===========================================================================

class TestCheckMaintainers:
    def test_real_maintainer_accepted(self):
        assert check_refs.check_maintainers("f.yaml", "pkg", [REAL_MAINTAINER]) == []

    def test_multiple_real_maintainers_accepted(self):
        second = {
            "name": "Kenzo Lobos-Tsunekawa",
            "email": "kenzo.lobos@tier4.jp",
            "github": "knzo25",
        }
        assert check_refs.check_maintainers("f.yaml", "pkg", [REAL_MAINTAINER, second]) == []

    def test_empty_list_accepted(self):
        assert check_refs.check_maintainers("f.yaml", "pkg", []) == []

    def test_none_list_accepted(self):
        # `maintainers or []` guards a None argument.
        assert check_refs.check_maintainers("f.yaml", "pkg", None) == []

    # --- placeholder NAME signals --------------------------------------
    @pytest.mark.parametrize("placeholder", ["TBD", "tbd", "ToDo", "N/A", "na", "None", "XXX"])
    def test_placeholder_name_rejected(self, placeholder):
        m = {"name": placeholder, "email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        assert len(errors) == 1
        assert "placeholder maintainer name" in errors[0]
        assert placeholder in errors[0]

    def test_name_check_is_case_insensitive(self):
        m = {"name": "Tbd", "email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        assert any("placeholder maintainer name" in e for e in errors)

    def test_missing_name_is_treated_as_placeholder(self):
        # name defaults to "" which is in PLACEHOLDER_NAMES.
        m = {"email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        assert any("placeholder maintainer name" in e for e in errors)

    def test_whitespace_only_name_is_placeholder(self):
        # name is .strip()'d, so "   " collapses to "" -> placeholder.
        m = {"name": "   ", "email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        assert any("placeholder maintainer name" in e for e in errors)

    # --- placeholder GITHUB signals ------------------------------------
    @pytest.mark.parametrize("placeholder", ["TBD", "tbd", "todo", "none", "xxx"])
    def test_placeholder_github_rejected(self, placeholder):
        m = {"name": "Real Name", "email": "real@tier4.jp", "github": placeholder}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        assert any("placeholder maintainer github" in e for e in errors)
        assert any(placeholder in e for e in errors)

    def test_missing_github_is_treated_as_placeholder(self):
        # github defaults to "" -> placeholder.
        m = {"name": "Real Name", "email": "real@tier4.jp"}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        assert any("placeholder maintainer github" in e for e in errors)

    # --- placeholder EMAIL signals -------------------------------------
    @pytest.mark.parametrize("email", ["tbd@example.com", "person@example.com", "x@example.org"])
    def test_placeholder_email_rejected(self, email):
        m = {"name": "Real Name", "email": email, "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        assert any("placeholder maintainer email" in e for e in errors)

    def test_email_check_is_case_insensitive(self):
        m = {"name": "Real Name", "email": "Person@Example.COM", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        # email is lowercased before the suffix check.
        assert any("placeholder maintainer email" in e for e in errors)
        assert any("person@example.com" in e for e in errors)

    def test_real_email_not_flagged(self):
        m = {"name": "Real Name", "email": "real@tier4.jp", "github": "realuser"}
        # only the example.* domains are placeholders; tier4.jp is real.
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        assert errors == []

    def test_email_substring_example_not_at_end_is_ok(self):
        # endswith, not "contains": example.com.evil.io must not match.
        m = {"name": "Real Name", "email": "a@example.com.tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        assert errors == []

    # --- multiple signals at once --------------------------------------
    def test_all_three_signals_fire_together(self):
        m = {"name": "TBD", "email": "tbd@example.com", "github": "tbd"}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [m])
        assert any("name" in e for e in errors)
        assert any("github" in e for e in errors)
        assert any("email" in e for e in errors)
        assert len(errors) == 3

    def test_error_message_contains_file_and_package(self):
        m = {"name": "TBD", "email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("distributions/jazzy.yaml", "my_pkg", [m])
        assert errors[0].startswith("distributions/jazzy.yaml::my_pkg:")

    def test_one_bad_maintainer_among_good_ones(self):
        bad = {"name": "TBD", "email": "real@tier4.jp", "github": "realuser"}
        errors = check_refs.check_maintainers("f.yaml", "pkg", [REAL_MAINTAINER, bad])
        assert len(errors) == 1
        assert "placeholder maintainer name" in errors[0]


# ===========================================================================
# check_ref — sha kind
# ===========================================================================

class TestCheckRefSha:
    def test_valid_sha_no_network(self):
        ref = {"kind": "sha", "value": GOOD_SHA}
        assert check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=False) == []

    def test_valid_sha_with_network_does_not_probe(self, monkeypatch):
        # sha kind returns before any ls_remote call even when network=True.
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote must not be called for sha"),
        )
        ref = {"kind": "sha", "value": GOOD_SHA}
        assert check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=True) == []

    def test_short_sha_rejected(self):
        ref = {"kind": "sha", "value": "abc123"}
        errors = check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=False)
        assert len(errors) == 1
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_uppercase_sha_rejected(self):
        ref = {"kind": "sha", "value": "A" * 40}
        errors = check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=False)
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_non_hex_sha_rejected(self):
        ref = {"kind": "sha", "value": "g" * 40}
        errors = check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=False)
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_too_long_sha_rejected(self):
        ref = {"kind": "sha", "value": "a" * 41}
        errors = check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=False)
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_missing_value_sha_rejected(self):
        # value defaults to "" -> not a valid sha.
        ref = {"kind": "sha"}
        errors = check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=False)
        assert "is not 40 lowercase hex chars" in errors[0]


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
        assert check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=False) == []

    def test_tag_no_network_skipped(self, monkeypatch):
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote must not run with network=False"),
        )
        ref = {"kind": "tag", "value": "1.0.0"}
        assert check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=False) == []

    def test_unknown_kind_no_network_skipped(self):
        ref = {"kind": "nonsense", "value": "x"}
        assert check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=False) == []

    def test_unknown_kind_with_network_is_noop(self, monkeypatch):
        # kind is neither sha/branch/tag: falls through both branches, no ls_remote.
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote must not run for unknown kind"),
        )
        ref = {"kind": "nonsense", "value": "x"}
        assert check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=True) == []

    def test_missing_kind_with_network_is_noop(self, monkeypatch):
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote must not run when kind missing"),
        )
        ref = {"value": "main"}
        assert check_refs.check_ref("f.yaml", "pkg", "repo", ref, network=True) == []


class TestCheckRefBranchTagNetwork:
    def test_branch_resolves(self, monkeypatch):
        calls = []

        def fake_ls_remote(repository, ref_filter, value):
            calls.append((repository, ref_filter, value))
            return True

        monkeypatch.setattr(check_refs, "ls_remote", fake_ls_remote)
        ref = {"kind": "branch", "value": "main"}
        assert check_refs.check_ref("f.yaml", "pkg", "https://r", ref, network=True) == []
        assert calls == [("https://r", "--heads", "main")]

    def test_branch_does_not_resolve(self, monkeypatch):
        monkeypatch.setattr(check_refs, "ls_remote", lambda *a, **k: False)
        ref = {"kind": "branch", "value": "ghost"}
        errors = check_refs.check_ref("f.yaml", "pkg", "https://r", ref, network=True)
        assert len(errors) == 1
        assert "branch ref 'ghost' does not resolve" in errors[0]
        assert "--heads" in errors[0]

    def test_tag_resolves(self, monkeypatch):
        calls = []

        def fake_ls_remote(repository, ref_filter, value):
            calls.append((repository, ref_filter, value))
            return True

        monkeypatch.setattr(check_refs, "ls_remote", fake_ls_remote)
        ref = {"kind": "tag", "value": "1.0.0"}
        assert check_refs.check_ref("f.yaml", "pkg", "https://r", ref, network=True) == []
        assert calls == [("https://r", "--tags", "1.0.0")]

    def test_tag_does_not_resolve(self, monkeypatch):
        monkeypatch.setattr(check_refs, "ls_remote", lambda *a, **k: False)
        ref = {"kind": "tag", "value": "0.2.1"}
        errors = check_refs.check_ref("f.yaml", "pkg", "https://r", ref, network=True)
        assert len(errors) == 1
        assert "tag ref '0.2.1' does not resolve" in errors[0]
        assert "--tags" in errors[0]


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
# check_file — integration over a temp distributions yaml
# ===========================================================================

class TestCheckFile:
    def _clean_body(self):
        return """\
            schema_version: "1"
            ros_distro: jazzy
            packages:
              autoware_livox_tag_filter:
                repository: https://github.com/autowarefoundation/autoware_livox_tag_filter
                maintainers:
                  - name: Ryohsuke Mitsudome
                    email: ryohsuke.mitsudome@tier4.jp
                    github: mitsudome-r
                ref:
                  kind: sha
                  value: "%s"
            """ % GOOD_SHA

    def test_clean_file_no_errors_offline(self, tmp_path):
        p = write_yaml(tmp_path, self._clean_body())
        assert check_refs.check_file(p, network=False) == []

    def test_clean_file_no_errors_with_network_sha_only(self, tmp_path, monkeypatch):
        # sha ref needs no ls_remote; network=True must still pass without one.
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote should not run for a sha ref"),
        )
        p = write_yaml(tmp_path, self._clean_body())
        assert check_refs.check_file(p, network=True) == []

    def test_placeholder_maintainer_flagged(self, tmp_path):
        body = """\
            schema_version: "1"
            packages:
              pkg_a:
                repository: https://github.com/x/y
                maintainers:
                  - name: TBD
                    email: tbd@example.com
                    github: tbd
                ref:
                  kind: sha
                  value: "%s"
            """ % GOOD_SHA
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False)
        # three placeholder signals from the single bad maintainer.
        assert len(errors) == 3
        assert all("pkg_a" in e for e in errors)

    def test_bad_ref_flagged(self, tmp_path):
        body = """\
            schema_version: "1"
            packages:
              pkg_a:
                repository: https://github.com/x/y
                maintainers:
                  - name: Real Name
                    email: real@tier4.jp
                    github: realuser
                ref:
                  kind: sha
                  value: "not-a-sha"
            """
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False)
        assert len(errors) == 1
        assert "is not 40 lowercase hex chars" in errors[0]

    def test_branch_ref_not_resolving_with_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(check_refs, "ls_remote", lambda *a, **k: False)
        body = """\
            schema_version: "1"
            packages:
              pkg_a:
                repository: https://github.com/x/y
                maintainers:
                  - name: Real Name
                    email: real@tier4.jp
                    github: realuser
                ref:
                  kind: branch
                  value: ghost-branch
            """
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=True)
        assert len(errors) == 1
        assert "branch ref 'ghost-branch' does not resolve" in errors[0]

    def test_ref_without_repository_is_not_checked(self, tmp_path, monkeypatch):
        # check_file only calls check_ref when BOTH ref and repository are truthy.
        monkeypatch.setattr(
            check_refs, "ls_remote",
            lambda *a, **k: pytest.fail("ls_remote should not run without a repository"),
        )
        body = """\
            schema_version: "1"
            packages:
              pkg_a:
                maintainers:
                  - name: Real Name
                    email: real@tier4.jp
                    github: realuser
                ref:
                  kind: branch
                  value: main
            """
        p = write_yaml(tmp_path, body)
        assert check_refs.check_file(p, network=True) == []

    def test_empty_file_no_errors(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        assert check_refs.check_file(p, network=False) == []

    def test_no_packages_key_no_errors(self, tmp_path):
        p = write_yaml(tmp_path, "schema_version: \"1\"\nros_distro: jazzy\n")
        assert check_refs.check_file(p, network=False) == []

    def test_top_level_not_a_dict_no_errors(self, tmp_path):
        # a YAML list at top level -> packages becomes {} -> no errors.
        p = write_yaml(tmp_path, "- a\n- b\n")
        assert check_refs.check_file(p, network=False) == []

    def test_null_package_spec_handled(self, tmp_path):
        # `spec or {}` guards a package mapped to null.
        body = """\
            schema_version: "1"
            packages:
              pkg_a:
            """
        p = write_yaml(tmp_path, body)
        assert check_refs.check_file(p, network=False) == []

    def test_yaml_parse_error_reported(self, tmp_path):
        p = tmp_path / "broken.yaml"
        p.write_text("packages: [unclosed\n")
        errors = check_refs.check_file(p, network=False)
        assert len(errors) == 1
        assert "YAML parse error" in errors[0]

    def test_multiple_packages_accumulate_errors(self, tmp_path):
        body = """\
            schema_version: "1"
            packages:
              good_pkg:
                repository: https://github.com/x/y
                maintainers:
                  - name: Real Name
                    email: real@tier4.jp
                    github: realuser
                ref:
                  kind: sha
                  value: "%s"
              bad_pkg:
                repository: https://github.com/x/z
                maintainers:
                  - name: TBD
                    email: real@tier4.jp
                    github: realuser
                ref:
                  kind: sha
                  value: "%s"
            """ % (GOOD_SHA, GOOD_SHA)
        p = write_yaml(tmp_path, body)
        errors = check_refs.check_file(p, network=False)
        assert len(errors) == 1
        assert "bad_pkg" in errors[0]
        assert all("good_pkg" not in e for e in errors)


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
