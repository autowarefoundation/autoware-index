"""Tests for scripts/apply_registration.py — issue body -> registry edit.

Imported as ``import apply_registration`` because conftest puts scripts/ on
sys.path. Covers section parsing, entry parsing/normalization, the insertion
logic (block located anywhere in the file, `repositories: {}` opening, the
round-trip tripwire), duplicate/DCO/distro rejections, and main().
"""

from pathlib import Path

import apply_registration as ar
import pytest
import yaml

ENTRY = """\
  demo_repo:
    url: https://example.com/acme/demo_repo
    ref:
      kind: branch
      value: main
    governance: community
    maintainers:
      - name: Jane Doe
        email: jane@acme.dev
        github: janedoe
    packages:
      demo_pkg:
        tags:
          - planning
"""


def issue_body(distro="jazzy", entry=ENTRY, dco="- [x] I certify the DCO.", fence=True):
    entry_block = f"```yaml\n{entry}```" if fence else entry
    return (
        f"### ROS distro\n\n{distro}\n\n"
        f"### Registry entry\n\n{entry_block}\n\n"
        f"### Developer Certificate of Origin\n\n{dco}\n"
    )


def registry_file(tmp_path, text=None):
    distributions = tmp_path / "distributions"
    distributions.mkdir()
    (distributions / "jazzy.yaml").write_text(
        text
        if text is not None
        else (
            'schema_version: "2"\n'
            "ros_distro: jazzy\n"
            "repositories:\n"
            "  existing_repo:\n"
            "    url: https://example.com/existing\n"
            "    packages:\n"
            "      existing_pkg:\n"
            "        tags: [sensing]\n"
        )
    )
    return distributions


# --------------------------------------------------------------------------- #
# split_sections / strip_fence / dedent_common
# --------------------------------------------------------------------------- #


def test_split_sections_maps_headings_to_bodies():
    sections = ar.split_sections(issue_body())
    assert sections["ros distro"] == "jazzy"
    assert "demo_repo:" in sections["registry entry"]
    assert sections["developer certificate of origin"].startswith("- [x]")


def test_split_sections_ignores_preamble_before_first_heading():
    assert ar.split_sections("hello\n### A\n\nbody") == {"a": "body"}


def test_strip_fence_removes_matching_fences_only():
    assert ar.strip_fence("```yaml\na: 1\n```") == "a: 1"
    assert ar.strip_fence("a: 1") == "a: 1"


def test_dedent_common_strips_shared_indent_only():
    assert ar.dedent_common("  a:\n    b: 1") == "a:\n  b: 1"
    assert ar.dedent_common("a:\n  b: 1") == "a:\n  b: 1"


# --------------------------------------------------------------------------- #
# parse_entry
# --------------------------------------------------------------------------- #


def test_parse_entry_happy_path():
    name, spec = ar.parse_entry(f"```yaml\n{ENTRY}```")
    assert name == "demo_repo"
    assert spec["url"] == "https://example.com/acme/demo_repo"
    assert spec["packages"]["demo_pkg"]["tags"] == ["planning"]


def test_parse_entry_accepts_any_consistent_base_indent():
    deeper = "\n".join("    " + l if l.strip() else "" for l in ENTRY.splitlines())
    name, _ = ar.parse_entry(deeper)
    assert name == "demo_repo"


def test_parse_entry_rejects_invalid_yaml():
    with pytest.raises(ar.RegistrationError, match="not valid YAML"):
        ar.parse_entry("  demo:\n url: [unclosed")


def test_parse_entry_rejects_multiple_entries():
    with pytest.raises(ar.RegistrationError, match="exactly ONE"):
        ar.parse_entry("a:\n  url: x\nb:\n  url: y\n")


def test_parse_entry_rejects_bad_name_and_non_mapping_spec():
    with pytest.raises(ar.RegistrationError, match="entry name"):
        ar.parse_entry("Bad-Name:\n  url: x\n")
    with pytest.raises(ar.RegistrationError, match="repository fields"):
        ar.parse_entry("demo_repo: just-a-string\n")


def test_parse_entry_rejects_empty():
    with pytest.raises(ar.RegistrationError, match="empty"):
        ar.parse_entry("```yaml\n\n```")


# --------------------------------------------------------------------------- #
# emit_entry — house style
# --------------------------------------------------------------------------- #


def test_emit_entry_two_space_base_and_indented_sequences():
    _, spec = ar.parse_entry(ENTRY)
    out = ar.emit_entry("demo_repo", spec)
    assert out.startswith("  demo_repo:\n    url:")
    # Block sequences are indented relative to their key (house style).
    assert "    maintainers:\n      - name: Jane Doe\n" in out
    assert "        tags:\n          - planning\n" in out


def test_emit_entry_round_trips():
    _, spec = ar.parse_entry(ENTRY)
    dumped = ar.emit_entry("demo_repo", spec)
    assert yaml.safe_load(dumped) == {"demo_repo": spec}


# --------------------------------------------------------------------------- #
# apply — end to end on a registry file
# --------------------------------------------------------------------------- #


def test_apply_appends_entry_and_round_trips(tmp_path):
    distributions = registry_file(tmp_path)
    distro, name = ar.apply(issue_body(), distributions)
    assert (distro, name) == ("jazzy", "demo_repo")
    doc = yaml.safe_load((distributions / "jazzy.yaml").read_text())
    assert set(doc["repositories"]) == {"existing_repo", "demo_repo"}
    assert doc["repositories"]["demo_repo"]["packages"]["demo_pkg"]["tags"] == ["planning"]


def test_apply_inserts_inside_repositories_block_not_at_eof(tmp_path):
    # `repositories:` is NOT the last top-level key: the entry must land
    # inside the block, before `ros_distro:`.
    distributions = registry_file(
        tmp_path,
        text=(
            'schema_version: "2"\n'
            "repositories:\n"
            "  existing_repo:\n"
            "    url: https://example.com/existing\n"
            "ros_distro: jazzy\n"
        ),
    )
    ar.apply(issue_body(), distributions)
    text = (distributions / "jazzy.yaml").read_text()
    assert text.index("demo_repo:") < text.index("ros_distro:")
    doc = yaml.safe_load(text)
    assert set(doc["repositories"]) == {"existing_repo", "demo_repo"}
    assert doc["ros_distro"] == "jazzy"


def test_apply_opens_an_empty_flow_repositories_block(tmp_path):
    distributions = registry_file(
        tmp_path,
        text='schema_version: "2"\nros_distro: jazzy\nrepositories: {}\n',
    )
    ar.apply(issue_body(), distributions)
    doc = yaml.safe_load((distributions / "jazzy.yaml").read_text())
    assert list(doc["repositories"]) == ["demo_repo"]


def test_apply_rejects_duplicate_entry_name(tmp_path):
    distributions = registry_file(tmp_path)
    body = issue_body(entry=ENTRY.replace("demo_repo:", "existing_repo:"))
    with pytest.raises(ar.RegistrationError, match="already a repository entry"):
        ar.apply(body, distributions)
    # File untouched on failure.
    assert "demo_repo" not in (distributions / "jazzy.yaml").read_text()


def test_apply_rejects_unticked_dco(tmp_path):
    distributions = registry_file(tmp_path)
    with pytest.raises(ar.RegistrationError, match="Certificate of Origin"):
        ar.apply(issue_body(dco="- [ ] I certify the DCO."), distributions)


def test_apply_rejects_missing_or_unknown_distro(tmp_path):
    distributions = registry_file(tmp_path)
    with pytest.raises(ar.RegistrationError, match="invalid ROS distro"):
        ar.apply(issue_body(distro="_No response_"), distributions)
    with pytest.raises(ar.RegistrationError, match="no registry file"):
        ar.apply(issue_body(distro="rolling"), distributions)


def test_apply_rejects_file_without_repositories(tmp_path):
    distributions = registry_file(tmp_path, text='schema_version: "2"\nros_distro: jazzy\n')
    with pytest.raises(ar.RegistrationError, match="repositories"):
        ar.apply(issue_body(), distributions)


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #


def test_main_success_prints_outputs(tmp_path, monkeypatch, capsys):
    distributions = registry_file(tmp_path)
    body_file = tmp_path / "body.md"
    body_file.write_text(issue_body())
    monkeypatch.setattr(
        "sys.argv",
        [
            "apply_registration.py",
            "--issue-body",
            str(body_file),
            "--distributions-dir",
            str(distributions),
        ],
    )
    assert ar.main() == 0
    out = capsys.readouterr().out
    assert "distro=jazzy" in out and "name=demo_repo" in out


def test_main_failure_diagnoses_to_stderr(tmp_path, monkeypatch, capsys):
    distributions = registry_file(tmp_path)
    body_file = tmp_path / "body.md"
    body_file.write_text(issue_body(dco="- [ ] nope"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "apply_registration.py",
            "--issue-body",
            str(body_file),
            "--distributions-dir",
            str(distributions),
        ],
    )
    assert ar.main() == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "distro=" not in captured.out
