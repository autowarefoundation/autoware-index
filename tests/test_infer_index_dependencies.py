"""Registration derives source selection from upstream package manifests."""

import subprocess

import apply_registration
import infer_index_dependencies as infer
import pytest
import yaml


def package_xml(name, dependencies=()):
    lines = ["<package format='3'>", f"<name>{name}</name>"]
    lines.extend(f"<depend>{dependency}</depend>" for dependency in dependencies)
    return "\n".join([*lines, "</package>"])


def git(*args):
    subprocess.run(["git", *map(str, args)], check=True, capture_output=True)


def test_registration_infers_index_sources_and_leaves_other_dependencies_to_rosdep(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git("init", "-q", "-b", "main", source)
    git("-C", source, "config", "user.name", "Test")
    git("-C", source, "config", "user.email", "test@example.org")
    consumer = source / "consumer"
    consumer.mkdir()
    (consumer / "package.xml").write_text(
        package_xml("consumer_pkg", ["indexed_pkg", "sibling_pkg", "ros_binary_pkg"])
    )
    sibling = source / "sibling"
    sibling.mkdir()
    (sibling / "package.xml").write_text(package_xml("sibling_pkg"))
    git("-C", source, "add", ".")
    git("-C", source, "commit", "-qm", "Add packages")
    git("-C", source, "checkout", "-qb", "candidate")

    distributions = tmp_path / "distributions"
    distributions.mkdir()
    path = distributions / "jazzy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "4",
                "ros_distro": "jazzy",
                "repositories": {
                    "library": {"packages": {"indexed_pkg": {"tags": ["common-library"]}}}
                },
            }
        )
    )
    entry = f"""\
  new_repo:
    url: {source.as_uri()}
    ref:
      kind: branch
      value: candidate
    governance: community
    maintainers:
      - {{name: Test, email: test@example.org, github: test}}
    packages:
      consumer_pkg:
        tags: [planning]
        index_dependencies: [incorrect_manual_entry]
      sibling_pkg:
        tags: [common-library]
"""
    body = (
        f"### ROS distro\n\njazzy\n\n### Registry entry\n\n```yaml\n{entry}```\n\n"
        "### Developer Certificate of Origin\n\n- [x] I certify.\n"
    )

    apply_registration.apply(body, distributions, infer_dependencies=True)
    packages = yaml.safe_load(path.read_text())["repositories"]["new_repo"]["packages"]
    assert packages["consumer_pkg"]["index_dependencies"] == ["indexed_pkg", "sibling_pkg"]
    assert "index_dependencies" not in packages["sibling_pkg"]


def test_duplicate_upstream_package_names_are_rejected(tmp_path):
    git("init", "-q", "-b", "main", tmp_path)
    for directory in ("one", "two"):
        package = tmp_path / directory
        package.mkdir()
        (package / "package.xml").write_text(package_xml("same_name"))
    git("-C", tmp_path, "add", ".")
    with pytest.raises(infer.DependencyInferenceError, match="more than one"):
        infer.read_package_manifests(tmp_path, "jazzy")


def test_registered_name_requires_matching_package_xml():
    spec = {"packages": {"missing_pkg": {"tags": ["planning"]}}}
    with pytest.raises(infer.DependencyInferenceError, match="has no package.xml"):
        infer.infer_dependencies(spec, set(), {})


def test_distro_condition_selects_only_active_dependency(tmp_path):
    git("init", "-q", "-b", "main", tmp_path)
    (tmp_path / "package.xml").write_text(
        "<package format='3'><name>consumer_pkg</name>"
        "<depend condition=\"$ROS_DISTRO == 'jazzy'\">jazzy_pkg</depend>"
        "<depend condition=\"$ROS_DISTRO == 'humble'\">humble_pkg</depend>"
        "<depend condition=\"$CUSTOM_MODE == 'enabled'\">custom_pkg</depend>"
        "</package>"
    )
    git("-C", tmp_path, "add", ".")
    assert infer.read_package_manifests(tmp_path, "jazzy") == {
        "consumer_pkg": {"jazzy_pkg", "custom_pkg"}
    }
