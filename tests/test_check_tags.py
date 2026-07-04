"""Tests for scripts/check_tags.py: the tag-vocabulary gate.

Focus: membership against the live vocabulary (did-you-mean suggestions,
no-close-match wording), deprecated-tag rejection naming the replacement, the
non-blocking tool-only ::warning:: nudge, the {file}::{repo}.{package} owner
format, the default distributions/*.yaml glob (what the pass_filenames:false
pre-commit hook relies on), exit codes and summary lines, plus the
real-committed-files test that pins vocabulary x registry consistency on
every PR regardless of validate.yaml's paths filters.
"""

import textwrap

import check_tags
import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

VOCAB_BODY = """\
    groups:
      pipeline: Autonomy pipeline
      operations: Development & operations
    tags:
      sensing:
        group: pipeline
        summary: Processes already-published sensor data.
      planning:
        group: pipeline
        summary: Mission, behavior, and motion planning.
      tool:
        group: operations
        summary: An executable developer utility you run.
    deprecated:
      launcher:
        replaced_by: [planning]
        note: Renamed.
      retired:
        replaced_by: []
    """

DISTRO_TEMPLATE = """\
    schema_version: "2"
    ros_distro: jazzy
    repositories:
      awesome_tools:
        url: https://github.com/example-org/awesome_tools
        ref: {{kind: tag, value: "1.2.0"}}
        governance: community
        maintainers:
          - {{name: Jane Doe, email: jane@example-org.dev, github: janedoe}}
        packages:
          autoware_a_filter:
            tags: {tags}
    """


def write_vocab(tmp_path, body=VOCAB_BODY, filename="tags.yaml"):
    p = tmp_path / filename
    p.write_text(textwrap.dedent(body))
    return p


def write_distro(tmp_path, tags="[sensing]", filename="jazzy.yaml"):
    p = tmp_path / filename
    p.write_text(textwrap.dedent(DISTRO_TEMPLATE).format(tags=tags))
    return p


def load_vocab(tmp_path):
    import registry_load

    return registry_load.load_vocabulary(write_vocab(tmp_path))


# ===========================================================================
# suggest
# ===========================================================================


class TestSuggest:
    def test_close_match_found(self):
        assert check_tags.suggest("sensign", {"sensing", "planning"}) == "sensing"

    def test_no_close_match(self):
        assert check_tags.suggest("slam", {"sensing", "planning"}) is None

    def test_deterministic_over_set_ordering(self):
        # Candidates are sorted before matching so the suggestion is stable.
        live = {"visualization", "localization"}
        assert check_tags.suggest("lokalization", live) == "localization"


# ===========================================================================
# check_package_tags
# ===========================================================================


class TestCheckPackageTags:
    def test_live_tags_accepted(self, tmp_path):
        vocab = load_vocab(tmp_path)
        assert check_tags.check_package_tags("f.yaml", "r.p", ["sensing", "tool"], vocab) == []

    def test_empty_and_none_accepted(self, tmp_path):
        # Cardinality is the JSON schema's job (minItems), not this check's.
        vocab = load_vocab(tmp_path)
        assert check_tags.check_package_tags("f.yaml", "r.p", [], vocab) == []
        assert check_tags.check_package_tags("f.yaml", "r.p", None, vocab) == []

    def test_unknown_tag_with_suggestion(self, tmp_path):
        vocab = load_vocab(tmp_path)
        errors = check_tags.check_package_tags("f.yaml", "r.p", ["sensign"], vocab)
        assert len(errors) == 1
        assert "f.yaml::r.p: unknown tag 'sensign' (did you mean: sensing?)" in errors[0]

    def test_unknown_tag_without_suggestion(self, tmp_path):
        vocab = load_vocab(tmp_path)
        errors = check_tags.check_package_tags("f.yaml", "r.p", ["slam"], vocab)
        assert len(errors) == 1
        assert "unknown tag 'slam' (no close match; see schema/tags.yaml)" in errors[0]

    def test_deprecated_tag_names_replacement(self, tmp_path):
        vocab = load_vocab(tmp_path)
        errors = check_tags.check_package_tags("f.yaml", "r.p", ["launcher"], vocab)
        assert len(errors) == 1
        assert "tag 'launcher' is deprecated; use: planning" in errors[0]

    def test_deprecated_tag_without_replacement(self, tmp_path):
        vocab = load_vocab(tmp_path)
        errors = check_tags.check_package_tags("f.yaml", "r.p", ["retired"], vocab)
        assert len(errors) == 1
        assert "tag 'retired' is deprecated (retired without replacement" in errors[0]

    def test_deprecated_is_not_suggested_for_typos(self, tmp_path):
        # A typo near a deprecated id must not resurrect it via did-you-mean.
        vocab = load_vocab(tmp_path)
        errors = check_tags.check_package_tags("f.yaml", "r.p", ["launchor"], vocab)
        assert len(errors) == 1
        assert "did you mean: launcher" not in errors[0]

    def test_multiple_problems_accumulate(self, tmp_path):
        vocab = load_vocab(tmp_path)
        errors = check_tags.check_package_tags(
            "f.yaml", "r.p", ["sensign", "launcher", "sensing"], vocab
        )
        assert len(errors) == 2

    @pytest.mark.parametrize("bad", [[["sensing"]], [{"a": "b"}], [None]])
    def test_non_string_tag_is_a_diagnostic_not_a_traceback(self, tmp_path, bad):
        # Shape defense for files that skip CI: unhashable/non-string items
        # must produce a {file}::{owner} error, never a TypeError.
        vocab = load_vocab(tmp_path)
        errors = check_tags.check_package_tags("f.yaml", "r.p", bad, vocab)
        assert len(errors) == 1
        assert "must be a string" in errors[0]


# ===========================================================================
# check_file
# ===========================================================================


class TestCheckFile:
    def test_clean_file(self, tmp_path):
        vocab = load_vocab(tmp_path)
        errors, warnings = check_tags.check_file(write_distro(tmp_path), vocab)
        assert errors == []
        assert warnings == []

    def test_owner_format_is_repo_dot_package(self, tmp_path):
        vocab = load_vocab(tmp_path)
        path = write_distro(tmp_path, tags="[nope]")
        errors, _ = check_tags.check_file(path, vocab)
        assert len(errors) == 1
        assert f"{path}::awesome_tools.autoware_a_filter:" in errors[0]

    def test_unsupported_schema_version_is_one_error(self, tmp_path):
        vocab = load_vocab(tmp_path)
        bad = tmp_path / "old.yaml"
        bad.write_text('schema_version: "1"\nros_distro: old\n')
        errors, warnings = check_tags.check_file(bad, vocab)
        assert len(errors) == 1
        assert "not supported" in errors[0]
        assert warnings == []

    def test_tool_only_tag_warns_but_does_not_error(self, tmp_path):
        vocab = load_vocab(tmp_path)
        errors, warnings = check_tags.check_file(write_distro(tmp_path, tags="[tool]"), vocab)
        assert errors == []
        assert len(warnings) == 1
        assert warnings[0].startswith("::warning::")
        assert "'tool' is the only tag" in warnings[0]
        assert "awesome_tools.autoware_a_filter" in warnings[0]

    def test_tool_with_domain_does_not_warn(self, tmp_path):
        vocab = load_vocab(tmp_path)
        errors, warnings = check_tags.check_file(
            write_distro(tmp_path, tags="[tool, planning]"), vocab
        )
        assert errors == []
        assert warnings == []

    @pytest.mark.parametrize("tags", ["sensing", "5"])
    def test_non_list_tags_is_a_diagnostic_not_a_traceback(self, tmp_path, tags):
        # `tags: sensing` (bare string) would otherwise iterate per character;
        # `tags: 5` would crash. Both must be one clean shape error.
        vocab = load_vocab(tmp_path)
        errors, warnings = check_tags.check_file(write_distro(tmp_path, tags=tags), vocab)
        assert len(errors) == 1
        assert "`tags` must be a list of tag ids" in errors[0]
        assert warnings == []


# ===========================================================================
# main
# ===========================================================================


class TestMain:
    def test_clean_run_exits_zero_with_summary(self, tmp_path, monkeypatch, capsys):
        vocab_path = write_vocab(tmp_path)
        distro = write_distro(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["check_tags.py", str(distro), "--vocabulary", str(vocab_path)],
        )
        assert check_tags.main() == 0
        err = capsys.readouterr().err
        assert "check_tags: all package tags are in the vocabulary (3 live, 2 deprecated)" in err

    def test_unknown_tag_exits_one_with_count(self, tmp_path, monkeypatch, capsys):
        vocab_path = write_vocab(tmp_path)
        distro = write_distro(tmp_path, tags="[sensign]")
        monkeypatch.setattr(
            "sys.argv",
            ["check_tags.py", str(distro), "--vocabulary", str(vocab_path)],
        )
        assert check_tags.main() == 1
        err = capsys.readouterr().err
        assert "did you mean: sensing?" in err
        assert "check_tags: 1 problem(s) found" in err

    def test_broken_vocabulary_is_a_hard_failure(self, tmp_path, monkeypatch, capsys):
        vocab_path = tmp_path / "tags.yaml"
        vocab_path.write_text("tags:\n  a:\n    summary: no groups key\n")
        distro = write_distro(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["check_tags.py", str(distro), "--vocabulary", str(vocab_path)],
        )
        assert check_tags.main() == 1
        err = capsys.readouterr().err
        assert "`groups` must be a non-empty mapping" in err
        assert "check_tags: 1 problem(s) found" in err

    def test_missing_vocabulary_is_a_hard_failure(self, tmp_path, monkeypatch, capsys):
        distro = write_distro(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["check_tags.py", str(distro), "--vocabulary", str(tmp_path / "absent.yaml")],
        )
        assert check_tags.main() == 1
        assert "cannot parse" in capsys.readouterr().err

    def test_multiple_files_accumulate(self, tmp_path, monkeypatch, capsys):
        vocab_path = write_vocab(tmp_path)
        d1 = write_distro(tmp_path, tags="[nope]", filename="jazzy.yaml")
        d2_body = textwrap.dedent(DISTRO_TEMPLATE).format(tags="[launcher]")
        d2 = tmp_path / "humble.yaml"
        d2.write_text(d2_body.replace("ros_distro: jazzy", "ros_distro: humble"))
        monkeypatch.setattr(
            "sys.argv",
            ["check_tags.py", str(d1), str(d2), "--vocabulary", str(vocab_path)],
        )
        assert check_tags.main() == 1
        err = capsys.readouterr().err
        assert "unknown tag 'nope'" in err
        assert "tag 'launcher' is deprecated; use: planning" in err
        assert "check_tags: 2 problem(s) found" in err

    def test_warnings_do_not_affect_exit_code(self, tmp_path, monkeypatch, capsys):
        vocab_path = write_vocab(tmp_path)
        distro = write_distro(tmp_path, tags="[tool]")
        monkeypatch.setattr(
            "sys.argv",
            ["check_tags.py", str(distro), "--vocabulary", str(vocab_path)],
        )
        assert check_tags.main() == 0
        err = capsys.readouterr().err
        assert "::warning::" in err
        assert "all package tags are in the vocabulary" in err

    def test_no_paths_and_empty_glob_is_a_hard_failure(self, tmp_path, monkeypatch, capsys):
        # Wrong cwd (no distributions/ dir) must never be a vacuous pass.
        vocab_path = write_vocab(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["check_tags.py", "--vocabulary", str(vocab_path)])
        assert check_tags.main() == 1
        assert "no distribution files found" in capsys.readouterr().err

    def test_no_paths_defaults_to_distributions_glob(self, tmp_path, monkeypatch, capsys):
        # The pass_filenames:false pre-commit hook runs the bare script from
        # the repo root: no positional paths means every distributions/*.yaml.
        vocab_path = write_vocab(tmp_path)
        distdir = tmp_path / "distributions"
        distdir.mkdir()
        write_distro(distdir, tags="[nope]")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["check_tags.py", "--vocabulary", str(vocab_path)])
        assert check_tags.main() == 1
        assert "unknown tag 'nope'" in capsys.readouterr().err


# ===========================================================================
# the committed vocabulary x the committed registry
# ===========================================================================


class TestRealCommittedFiles:
    def test_registry_conforms_to_vocabulary(self, repo_root, monkeypatch, capsys):
        # The every-PR drift net: validate.yaml's paths filter can be dodged,
        # but plain pytest runs everywhere; the committed registry must
        # always pass against the committed vocabulary.
        distros = sorted((repo_root / "distributions").glob("*.yaml"))
        assert distros, "no distribution files found"
        monkeypatch.setattr(
            "sys.argv",
            [
                "check_tags.py",
                *[str(p) for p in distros],
                "--vocabulary",
                str(repo_root / "schema" / "tags.yaml"),
            ],
        )
        assert check_tags.main() == 0
        assert "all package tags are in the vocabulary" in capsys.readouterr().err
