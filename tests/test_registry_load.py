"""Tests for scripts/registry_load.py (the shared version-gated loader).

Every reader of distributions/*.yaml goes through this module, so the gate's
behavior IS the compatibility contract: an unsupported schema_version must be
a hard RegistryError everywhere, never a silently-empty result.
"""

import pytest
import registry_load as m

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
V2_DOC = """
schema_version: "2"
ros_distro: jazzy
repositories:
  awesome_tools:
    url: https://github.com/example-org/awesome_tools
    ref: {kind: tag, value: "1.2.0"}
    governance: community
    maintainers:
      - {name: Jane Doe, email: jane@example-org.dev, github: janedoe}
    packages:
      autoware_a_filter:
        tags: [sensing]
        description: Filters things.
      zz_planner_b:
        tags: [planning]
        maintainers:
          - {name: Bob Roe, email: bob@example-org.dev, github: bobroe}
"""


def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# load_distribution — the gate
# --------------------------------------------------------------------------
def test_load_distribution_accepts_v2(tmp_path):
    doc = m.load_distribution(write(tmp_path, "jazzy.yaml", V2_DOC))
    assert doc["ros_distro"] == "jazzy"
    assert set(doc["repositories"]) == {"awesome_tools"}


def test_load_distribution_rejects_v1(tmp_path):
    path = write(
        tmp_path,
        "jazzy.yaml",
        'schema_version: "1"\nros_distro: jazzy\npackages: {}\n',
    )
    with pytest.raises(m.RegistryError) as exc:
        m.load_distribution(path)
    assert "not supported" in str(exc.value)
    assert "'1'" in str(exc.value)


def test_load_distribution_rejects_missing_schema_version(tmp_path):
    path = write(tmp_path, "jazzy.yaml", "ros_distro: jazzy\nrepositories: {}\n")
    with pytest.raises(m.RegistryError) as exc:
        m.load_distribution(path)
    assert "not supported" in str(exc.value)


def test_load_distribution_rejects_non_mapping(tmp_path):
    path = write(tmp_path, "jazzy.yaml", "- just\n- a\n- list\n")
    with pytest.raises(m.RegistryError) as exc:
        m.load_distribution(path)
    assert "mapping" in str(exc.value)


def test_load_distribution_rejects_yaml_parse_error(tmp_path):
    path = write(tmp_path, "jazzy.yaml", "repositories: {unclosed\n")
    with pytest.raises(m.RegistryError) as exc:
        m.load_distribution(path)
    assert "cannot parse" in str(exc.value)


def test_load_distribution_rejects_missing_repositories(tmp_path):
    path = write(tmp_path, "jazzy.yaml", 'schema_version: "2"\nros_distro: jazzy\n')
    with pytest.raises(m.RegistryError) as exc:
        m.load_distribution(path)
    assert "repositories" in str(exc.value)


def test_load_distribution_rejects_non_mapping_repositories(tmp_path):
    path = write(
        tmp_path, "jazzy.yaml", 'schema_version: "2"\nros_distro: jazzy\nrepositories: []\n'
    )
    with pytest.raises(m.RegistryError):
        m.load_distribution(path)


def test_load_distribution_rejects_non_string_ref_value(tmp_path):
    # YAML types `value: 1.20` as a float; str() would mangle it to "1.2" and
    # the sweep would check out the wrong ref. The uniform gate rejects it.
    path = write(
        tmp_path,
        "jazzy.yaml",
        'schema_version: "2"\nros_distro: jazzy\nrepositories:\n'
        "  r:\n    url: u\n    ref: {kind: tag, value: 1.20}\n"
        "    packages: {p: {tags: [a]}}\n",
    )
    with pytest.raises(m.RegistryError) as exc:
        m.load_distribution(path)
    assert "must be a string" in str(exc.value)


def test_load_distributions_dir_sorted_and_gated(tmp_path):
    write(tmp_path, "humble.yaml", V2_DOC.replace("jazzy", "humble"))
    write(tmp_path, "jazzy.yaml", V2_DOC)
    loaded = m.load_distributions_dir(tmp_path)
    assert [p.name for p, _ in loaded] == ["humble.yaml", "jazzy.yaml"]

    write(tmp_path, "ancient.yaml", 'schema_version: "1"\nros_distro: ancient\n')
    with pytest.raises(m.RegistryError):
        m.load_distributions_dir(tmp_path)


# --------------------------------------------------------------------------
# canonical_url
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "variant",
    [
        "https://github.com/example-org/awesome_tools",
        "https://github.com/example-org/awesome_tools.git",
        "https://github.com/example-org/awesome_tools/",
        "https://github.com/Example-Org/Awesome_Tools",
        "http://github.com/example-org/awesome_tools",
        "git@github.com:example-org/awesome_tools.git",
        "ssh://git@github.com/example-org/awesome_tools",
        # userinfo and explicit ports are dropped (self-hosted forges):
        "https://oauth2:token@github.com/example-org/awesome_tools.git",
        "ssh://git@github.com:22/example-org/awesome_tools",
        "https://github.com:443/example-org/awesome_tools",
    ],
)
def test_canonical_url_folds_spelling_variants(variant):
    assert m.canonical_url(variant) == "github.com/example-org/awesome_tools"


def test_canonical_url_nonstandard_port_folds_with_plain_form():
    # GitLab-style ssh on a non-22 port still names the same repository as
    # the https spelling; ports are deliberately dropped.
    assert m.canonical_url("ssh://git@gitlab.example.com:2222/org/repo.git") == m.canonical_url(
        "https://gitlab.example.com/org/repo"
    )


def test_canonical_url_distinct_repos_stay_distinct():
    a = m.canonical_url("https://github.com/org-a/tools")
    b = m.canonical_url("https://github.com/org-b/tools")
    assert a != b


# --------------------------------------------------------------------------
# flatten_packages — the computed package -> repository inverse index
# --------------------------------------------------------------------------
def test_flatten_packages_one_record_per_package(tmp_path):
    doc = m.load_distribution(write(tmp_path, "jazzy.yaml", V2_DOC))
    records = m.flatten_packages(doc)
    assert [r["package"] for r in records] == ["autoware_a_filter", "zz_planner_b"]
    for r in records:
        assert r["distro"] == "jazzy"
        assert r["repo_name"] == "awesome_tools"
        assert r["repository"] == "https://github.com/example-org/awesome_tools"
        assert r["ref"] == {"kind": "tag", "value": "1.2.0"}
        assert r["governance"] == "community"


def test_flatten_packages_maintainer_override_and_default(tmp_path):
    doc = m.load_distribution(write(tmp_path, "jazzy.yaml", V2_DOC))
    by_name = {r["package"]: r for r in m.flatten_packages(doc)}
    # autoware_a_filter inherits the repo-level default ...
    assert by_name["autoware_a_filter"]["maintainers"][0]["github"] == "janedoe"
    # ... zz_planner_b overrides with its own list.
    assert by_name["zz_planner_b"]["maintainers"][0]["github"] == "bobroe"


def test_flatten_packages_description_defaults_empty(tmp_path):
    doc = m.load_distribution(write(tmp_path, "jazzy.yaml", V2_DOC))
    by_name = {r["package"]: r for r in m.flatten_packages(doc)}
    assert by_name["autoware_a_filter"]["description"] == "Filters things."
    assert by_name["zz_planner_b"]["description"] == ""


def test_flatten_packages_distro_override():
    doc = {"ros_distro": "jazzy", "repositories": {"r": {"url": "u", "packages": {"p": {}}}}}
    records = m.flatten_packages(doc, distro="humble")
    assert records[0]["distro"] == "humble"


def test_flatten_packages_tolerates_sparse_specs():
    # Null repo spec / null package spec / missing packages: no crash, sane defaults.
    doc = {
        "ros_distro": "jazzy",
        "repositories": {
            "bare": None,
            "nopkgs": {"url": "u"},
            "r": {"url": "u2", "packages": {"p": None}},
        },
    }
    records = m.flatten_packages(doc)
    assert [r["package"] for r in records] == ["p"]
    assert records[0]["tags"] == []
    assert records[0]["maintainers"] == []
    assert records[0]["governance"] == "community"
