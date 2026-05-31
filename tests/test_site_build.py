"""Tests for site/build.py — the browse-data exporter / site assembler.

Imported as ``import build`` because conftest puts site/ on sys.path.

Covers the real signatures verified by reading build.py:
  semver_key, parse_description, load_distributions, load_history,
  load_metadata, summarize, build_packages, and main().
"""

import json

import pytest

import build


# --------------------------------------------------------------------------- #
# semver_key
# --------------------------------------------------------------------------- #


def test_semver_key_numeric_not_lexical_ordering():
    versions = ["1.10.0", "1.7.0", "1.6.0", "1.8.0"]
    ordered = sorted(versions, key=build.semver_key)
    assert ordered == ["1.6.0", "1.7.0", "1.8.0", "1.10.0"]


def test_semver_key_lexical_would_be_wrong():
    # Sanity: lexical sort gets it wrong, numeric (semver_key) gets it right.
    assert sorted(["1.10.0", "1.9.0"]) == ["1.10.0", "1.9.0"]
    assert sorted(["1.10.0", "1.9.0"], key=build.semver_key) == ["1.9.0", "1.10.0"]


def test_semver_key_returns_tuple_of_pairs_for_numeric():
    assert build.semver_key("1.2.3") == ((0, 1), (0, 2), (0, 3))


def test_semver_key_non_numeric_parts_sort_last():
    # Non-numeric pieces are tagged (1, piece) so they sort after numeric (0, n).
    numeric = build.semver_key("1.0.0")
    with_suffix = build.semver_key("1.0.rc1")
    assert numeric < with_suffix
    assert with_suffix == ((0, 1), (0, 0), (1, "rc1"))


def test_semver_key_accepts_non_string():
    # str(version) coerces; a plain int has no dots so it's a single numeric part.
    assert build.semver_key(5) == ((0, 5),)


# --------------------------------------------------------------------------- #
# parse_description
# --------------------------------------------------------------------------- #


def test_parse_description_basic():
    xml = "<package><description>Hello world</description></package>"
    assert build.parse_description(xml) == "Hello world"


def test_parse_description_collapses_internal_whitespace_and_newlines():
    xml = (
        "<package><description>\n"
        "    A package that does\n"
        "    many   things   well.\n"
        "  </description></package>"
    )
    assert build.parse_description(xml) == "A package that does many things well."


def test_parse_description_includes_nested_text_via_itertext():
    xml = (
        "<package><description>see <a href='x'>the link</a> here"
        "</description></package>"
    )
    assert build.parse_description(xml) == "see the link here"


def test_parse_description_missing_description_returns_empty():
    xml = "<package><name>foo</name></package>"
    assert build.parse_description(xml) == ""


def test_parse_description_invalid_xml_returns_empty():
    assert build.parse_description("not xml <<<") == ""


def test_parse_description_empty_description_returns_empty():
    xml = "<package><description>   </description></package>"
    assert build.parse_description(xml) == ""


# --------------------------------------------------------------------------- #
# load_distributions
# --------------------------------------------------------------------------- #


def test_load_distributions_flattens_packages(tmp_path):
    (tmp_path / "humble.yaml").write_text(
        "ros_distro: humble\n"
        "packages:\n"
        "  pkg_a:\n"
        "    repository: https://example.com/a\n"
        "    description: Package A\n"
        "    governance: core\n"
        "    tags: [planning]\n"
        "    maintainers: [alice]\n"
        "    ref: {type: branch, name: main}\n"
    )
    regs = build.load_distributions(tmp_path)
    assert len(regs) == 1
    reg = regs[0]
    assert reg == {
        "distro": "humble",
        "name": "pkg_a",
        "repository": "https://example.com/a",
        "description": "Package A",
        "governance": "core",
        "tags": ["planning"],
        "maintainers": ["alice"],
        "ref": {"type": "branch", "name": "main"},
    }


def test_load_distributions_defaults_for_sparse_spec(tmp_path):
    # A package whose spec is empty (None) should get all defaults filled in.
    (tmp_path / "humble.yaml").write_text("ros_distro: humble\npackages:\n  bare:\n")
    regs = build.load_distributions(tmp_path)
    assert regs == [
        {
            "distro": "humble",
            "name": "bare",
            "repository": "",
            "description": "",
            "governance": "community",
            "tags": [],
            "maintainers": [],
            "ref": {},
        }
    ]


def test_load_distributions_distro_falls_back_to_stem(tmp_path):
    # No ros_distro key -> distro derived from filename stem.
    (tmp_path / "jazzy.yaml").write_text("packages:\n  p:\n    repository: r\n")
    regs = build.load_distributions(tmp_path)
    assert regs[0]["distro"] == "jazzy"


def test_load_distributions_empty_file_and_no_packages(tmp_path):
    (tmp_path / "empty.yaml").write_text("")
    (tmp_path / "nopkgs.yaml").write_text("ros_distro: rolling\n")
    regs = build.load_distributions(tmp_path)
    assert regs == []


def test_load_distributions_multiple_files_each_package(tmp_path):
    (tmp_path / "humble.yaml").write_text(
        "ros_distro: humble\npackages:\n  a:\n    repository: ra\n  b:\n    repository: rb\n"
    )
    (tmp_path / "jazzy.yaml").write_text(
        "ros_distro: jazzy\npackages:\n  c:\n    repository: rc\n"
    )
    regs = build.load_distributions(tmp_path)
    by_name = {(r["distro"], r["name"]): r for r in regs}
    assert set(by_name) == {("humble", "a"), ("humble", "b"), ("jazzy", "c")}


def test_load_distributions_ignores_non_yaml(tmp_path):
    (tmp_path / "readme.txt").write_text("ros_distro: x\npackages:\n  p:\n")
    (tmp_path / "humble.yml").write_text("ros_distro: humble\npackages:\n  q:\n")
    # Only *.yaml is globbed, so both of the above are ignored.
    assert build.load_distributions(tmp_path) == []


# --------------------------------------------------------------------------- #
# load_history
# --------------------------------------------------------------------------- #


def test_load_history_reads_ndjson(tmp_path):
    distro_dir = tmp_path / "humble"
    distro_dir.mkdir()
    (distro_dir / "pkg.ndjson").write_text(
        json.dumps({"status": "pass", "at": "2024-01-01"})
        + "\n"
        + json.dumps({"status": "fail", "at": "2024-02-01"})
        + "\n"
    )
    history = build.load_history(tmp_path)
    assert set(history) == {("humble", "pkg")}
    records = history[("humble", "pkg")]
    assert [r["status"] for r in records] == ["pass", "fail"]


def test_load_history_skips_blank_lines(tmp_path):
    distro_dir = tmp_path / "humble"
    distro_dir.mkdir()
    (distro_dir / "pkg.ndjson").write_text(
        "\n"
        + json.dumps({"status": "pass"})
        + "\n\n   \n"
        + json.dumps({"status": "fail"})
        + "\n"
    )
    records = build.load_history(tmp_path)[("humble", "pkg")]
    assert len(records) == 2


def test_load_history_missing_dir_returns_empty(tmp_path):
    assert build.load_history(tmp_path / "does-not-exist") == {}


def test_load_history_empty_path_returns_empty():
    # Falsy path (empty Path-like) -> short-circuits to {}.
    assert build.load_history("") == {}


def test_load_history_multiple_distros(tmp_path):
    for distro in ("humble", "jazzy"):
        d = tmp_path / distro
        d.mkdir()
        (d / "p.ndjson").write_text(json.dumps({"status": "pass"}) + "\n")
    history = build.load_history(tmp_path)
    assert set(history) == {("humble", "p"), ("jazzy", "p")}


# --------------------------------------------------------------------------- #
# load_metadata
# --------------------------------------------------------------------------- #


def test_load_metadata_reads_package_xml(tmp_path):
    d = tmp_path / "humble"
    d.mkdir()
    (d / "pkg.xml").write_text(
        "<package><description>Cached desc</description></package>",
        encoding="utf-8",
    )
    metadata = build.load_metadata(tmp_path)
    assert metadata == {("humble", "pkg"): "Cached desc"}


def test_load_metadata_normalizes_whitespace(tmp_path):
    d = tmp_path / "jazzy"
    d.mkdir()
    (d / "p.xml").write_text(
        "<package><description>\n  multi\n  line\n</description></package>",
        encoding="utf-8",
    )
    assert build.load_metadata(tmp_path)[("jazzy", "p")] == "multi line"


def test_load_metadata_missing_dir_returns_empty(tmp_path):
    assert build.load_metadata(tmp_path / "nope") == {}


def test_load_metadata_empty_path_returns_empty():
    assert build.load_metadata("") == {}


def test_load_metadata_ignores_top_level_xml(tmp_path):
    # Glob is */*.xml, so an xml directly in the root (no distro dir) is skipped.
    (tmp_path / "loose.xml").write_text(
        "<package><description>nope</description></package>", encoding="utf-8"
    )
    assert build.load_metadata(tmp_path) == {}


# --------------------------------------------------------------------------- #
# summarize — the last-green / current-status logic
# --------------------------------------------------------------------------- #


def test_summarize_empty():
    assert build.summarize([]) == {
        "current_status": "unknown",
        "last_green": None,
        "last_tested_at": None,
        "versions": [],
    }


def test_summarize_latest_fail_with_earlier_pass():
    # Earlier pass on 1.6.0, latest record is a fail on 1.7.0.
    # current_status should be fail (latest), last_green the earlier passing version.
    records = [
        {"status": "pass", "at": "2024-01-01", "autoware_version": "1.6.0"},
        {"status": "fail", "at": "2024-02-01", "autoware_version": "1.7.0"},
    ]
    result = build.summarize(records)
    assert result["current_status"] == "fail"
    assert result["last_green"] == "1.6.0"
    assert result["last_tested_at"] == "2024-02-01"


def test_summarize_all_pass():
    records = [
        {"status": "pass", "at": "2024-01-01", "autoware_version": "1.6.0"},
        {"status": "pass", "at": "2024-02-01", "autoware_version": "1.7.0"},
    ]
    result = build.summarize(records)
    assert result["current_status"] == "pass"
    # last_green is the most recent (by time) passing version.
    assert result["last_green"] == "1.7.0"


def test_summarize_all_fail_has_no_last_green():
    records = [
        {"status": "fail", "at": "2024-01-01", "autoware_version": "1.6.0"},
        {"status": "fail", "at": "2024-02-01", "autoware_version": "1.7.0"},
    ]
    result = build.summarize(records)
    assert result["current_status"] == "fail"
    assert result["last_green"] is None


def test_summarize_last_green_by_time_not_version():
    # last_green follows the latest passing record by `at`, even if an older
    # pass had a numerically larger version.
    records = [
        {"status": "pass", "at": "2024-03-01", "autoware_version": "1.6.0"},
        {"status": "pass", "at": "2024-01-01", "autoware_version": "1.10.0"},
    ]
    # After sorting by `at`: 1.10.0 (Jan) then 1.6.0 (Mar) -> last green = 1.6.0.
    assert build.summarize(records)["last_green"] == "1.6.0"


def test_summarize_latest_per_version_most_recent_wins():
    # Two records for the same version; the later `at` should populate the cell.
    records = [
        {"status": "fail", "at": "2024-01-01", "autoware_version": "1.6.0",
         "resolved_sha": "old"},
        {"status": "pass", "at": "2024-02-01", "autoware_version": "1.6.0",
         "resolved_sha": "new"},
    ]
    result = build.summarize(records)
    assert len(result["versions"]) == 1
    cell = result["versions"][0]
    assert cell["autoware_version"] == "1.6.0"
    assert cell["status"] == "pass"
    assert cell["resolved_sha"] == "new"


def test_summarize_versions_sorted_descending_by_semver():
    records = [
        {"status": "pass", "at": "2024-01-01", "autoware_version": "1.6.0"},
        {"status": "pass", "at": "2024-02-01", "autoware_version": "1.10.0"},
        {"status": "pass", "at": "2024-03-01", "autoware_version": "1.8.0"},
    ]
    vers = [v["autoware_version"] for v in build.summarize(records)["versions"]]
    assert vers == ["1.10.0", "1.8.0", "1.6.0"]


def test_summarize_version_cell_fields_and_defaults():
    records = [
        {
            "status": "pass",
            "at": "2024-01-01",
            "autoware_version": "1.6.0",
            "ref_at_test": {"name": "main"},
            "resolved_sha": "abc",
            "actions_run_url": "http://ci/1",
        }
    ]
    cell = build.summarize(records)["versions"][0]
    assert cell == {
        "autoware_version": "1.6.0",
        "status": "pass",
        "ref_at_test": {"name": "main"},
        "resolved_sha": "abc",
        "at": "2024-01-01",
        "actions_run_url": "http://ci/1",
    }


def test_summarize_missing_fields_use_defaults():
    # A record missing autoware_version/status keys still summarizes safely.
    records = [{"at": "2024-01-01"}]
    result = build.summarize(records)
    assert result["current_status"] == "unknown"
    cell = result["versions"][0]
    assert cell["autoware_version"] == "?"
    assert cell["status"] == "unknown"
    assert cell["ref_at_test"] == {}
    assert cell["resolved_sha"] == ""


# --------------------------------------------------------------------------- #
# build_packages — the three-way join + description precedence
# --------------------------------------------------------------------------- #


def _reg(distro="humble", name="pkg", description="", **extra):
    base = {
        "distro": distro,
        "name": name,
        "repository": "",
        "description": description,
        "governance": "community",
        "tags": [],
        "maintainers": [],
        "ref": {},
    }
    base.update(extra)
    return base


def test_build_packages_registry_description_overrides_cached():
    reg = _reg(description="Registry override")
    metadata = {("humble", "pkg"): "Cached package.xml desc"}
    packages = build.build_packages([reg], history={}, metadata=metadata)
    assert len(packages) == 1
    assert packages[0]["description"] == "Registry override"


def test_build_packages_falls_back_to_cached_when_registry_absent():
    reg = _reg(description="")  # no registry description
    metadata = {("humble", "pkg"): "Cached package.xml desc"}
    packages = build.build_packages([reg], history={}, metadata=metadata)
    assert packages[0]["description"] == "Cached package.xml desc"


def test_build_packages_empty_description_when_neither_present():
    reg = _reg(description="")
    packages = build.build_packages([reg], history={}, metadata={})
    assert packages[0]["description"] == ""


def test_build_packages_merges_summary_fields():
    reg = _reg()
    history = {
        ("humble", "pkg"): [
            {"status": "pass", "at": "2024-01-01", "autoware_version": "1.6.0"},
        ]
    }
    packages = build.build_packages([reg], history=history, metadata={})
    pkg = packages[0]
    assert pkg["current_status"] == "pass"
    assert pkg["last_green"] == "1.6.0"
    assert pkg["last_tested_at"] == "2024-01-01"
    assert pkg["governance"] == "community"  # registration field preserved


def test_build_packages_no_history_is_unknown():
    packages = build.build_packages([_reg()], history={}, metadata={})
    assert packages[0]["current_status"] == "unknown"
    assert packages[0]["versions"] == []


def test_build_packages_sorted_by_name_then_distro():
    regs = [
        _reg(distro="jazzy", name="b"),
        _reg(distro="humble", name="b"),
        _reg(distro="humble", name="a"),
    ]
    packages = build.build_packages(regs, history={}, metadata={})
    assert [(p["name"], p["distro"]) for p in packages] == [
        ("a", "humble"),
        ("b", "humble"),
        ("b", "jazzy"),
    ]


def test_build_packages_summarize_overrides_description_key_order():
    # summarize() does not emit a "description" key, and the explicit
    # description kwarg comes last in the dict merge — confirm it wins.
    reg = _reg(description="from registry")
    packages = build.build_packages([reg], history={}, metadata={})
    assert packages[0]["description"] == "from registry"


# --------------------------------------------------------------------------- #
# main() — end-to-end assembly into --out
# --------------------------------------------------------------------------- #


def test_main_writes_data_json_and_copies_assets(tmp_path, monkeypatch, capsys):
    distributions = tmp_path / "distributions"
    distributions.mkdir()
    (distributions / "humble.yaml").write_text(
        "ros_distro: humble\n"
        "packages:\n"
        "  pkg_a:\n"
        "    repository: https://example.com/a\n"
        "    description: Registry A\n"
    )

    history = tmp_path / "history"
    (history / "humble").mkdir(parents=True)
    (history / "humble" / "pkg_a.ndjson").write_text(
        json.dumps(
            {"status": "pass", "at": "2024-01-01", "autoware_version": "1.6.0"}
        )
        + "\n"
    )

    metadata = tmp_path / "metadata"
    (metadata / "humble").mkdir(parents=True)
    (metadata / "humble" / "pkg_a.xml").write_text(
        "<package><description>Cached A</description></package>",
        encoding="utf-8",
    )

    out_dir = tmp_path / "out"

    argv = [
        "build.py",
        "--distributions-dir",
        str(distributions),
        "--history-dir",
        str(history),
        "--metadata-dir",
        str(metadata),
        "--out",
        str(out_dir),
        "--built-at",
        "2024-12-31T00:00:00Z",
    ]
    monkeypatch.setattr("sys.argv", argv)

    build.main()

    data = json.loads((out_dir / "data.json").read_text())
    assert data["built_at"] == "2024-12-31T00:00:00Z"
    assert len(data["packages"]) == 1
    pkg = data["packages"][0]
    assert pkg["name"] == "pkg_a"
    # Registry description wins over the cached package.xml.
    assert pkg["description"] == "Registry A"
    assert pkg["current_status"] == "pass"
    assert pkg["last_green"] == "1.6.0"

    # Static assets copied alongside data.json.
    for asset in build.STATIC_ASSETS:
        assert (out_dir / asset).is_file()

    out = capsys.readouterr().out
    assert "built 1 package(s)" in out
    assert "1 history record(s)" in out


def test_main_falls_back_to_cached_description(tmp_path, monkeypatch):
    distributions = tmp_path / "distributions"
    distributions.mkdir()
    # No description in the registry entry -> should use cached package.xml.
    (distributions / "humble.yaml").write_text(
        "ros_distro: humble\npackages:\n  pkg_a:\n    repository: r\n"
    )
    metadata = tmp_path / "metadata"
    (metadata / "humble").mkdir(parents=True)
    (metadata / "humble" / "pkg_a.xml").write_text(
        "<package><description>Cached fallback</description></package>",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv",
        [
            "build.py",
            "--distributions-dir",
            str(distributions),
            "--metadata-dir",
            str(metadata),
            "--out",
            str(out_dir),
        ],
    )
    build.main()
    data = json.loads((out_dir / "data.json").read_text())
    assert data["packages"][0]["description"] == "Cached fallback"
    assert data["built_at"] == "unknown"  # default when --built-at not given


def test_main_without_history_or_metadata(tmp_path, monkeypatch):
    distributions = tmp_path / "distributions"
    distributions.mkdir()
    (distributions / "humble.yaml").write_text(
        "ros_distro: humble\npackages:\n  pkg_a:\n    repository: r\n"
    )
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv",
        [
            "build.py",
            "--distributions-dir",
            str(distributions),
            "--out",
            str(out_dir),
        ],
    )
    build.main()
    data = json.loads((out_dir / "data.json").read_text())
    assert data["packages"][0]["current_status"] == "unknown"
    assert (out_dir / "data.json").is_file()
