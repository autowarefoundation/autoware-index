"""Registry and sweep contracts for package-level index dependencies."""

import json
from pathlib import Path

import check_refs
import diff_matrix
import jsonschema
import registry_load
import sweep_matrix
import yaml

SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "distribution.schema.json"


def distribution(*, target_dependencies=None, dependency_ref="v1"):
    target_spec = {"tags": ["planning"]}
    if target_dependencies is not None:
        target_spec["index_dependencies"] = target_dependencies
    return {
        "schema_version": "3",
        "ros_distro": "jazzy",
        "repositories": {
            "consumer": {
                "url": "https://example.org/consumer",
                "ref": {"kind": "tag", "value": "v1"},
                "governance": "community",
                "maintainers": [{"name": "A", "email": "a@example.org", "github": "a"}],
                "packages": {"consumer_pkg": target_spec},
            },
            "library": {
                "url": "https://example.org/library",
                "ref": {"kind": "branch", "value": dependency_ref},
                "governance": "community",
                "maintainers": [{"name": "B", "email": "b@example.org", "github": "b"}],
                "packages": {"library_pkg": {"tags": ["common-library"]}},
            },
        },
    }


def write_distribution(directory, doc):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "jazzy.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def test_v3_schema_and_legacy_v2_gate(tmp_path):
    schema = json.loads(SCHEMA.read_text())
    doc = distribution(target_dependencies=["library_pkg"])
    jsonschema.validate(doc, schema)
    path = write_distribution(tmp_path / "d", doc)
    assert registry_load.load_distribution(path)["schema_version"] == "3"
    assert registry_load.flatten_packages(doc)[0]["index_dependencies"] == ["library_pkg"]

    doc["schema_version"] = "2"
    assert not jsonschema.Draft202012Validator(schema).is_valid(doc)
    path = write_distribution(tmp_path / "d", doc)
    try:
        registry_load.load_distribution(path)
    except registry_load.RegistryError as exc:
        assert "requires schema_version '3'" in str(exc)
    else:
        raise AssertionError("v2 dependencies must fail loudly")


def test_semantic_targets_and_cycles(tmp_path):
    doc = distribution(target_dependencies=["missing_pkg"])
    path = write_distribution(tmp_path / "d", doc)
    assert "not registered" in " ".join(check_refs.check_file(path, False, {}))

    doc["repositories"]["consumer"]["packages"]["consumer_pkg"]["index_dependencies"] = [
        "library_pkg"
    ]
    doc["repositories"]["library"]["packages"]["library_pkg"]["index_dependencies"] = [
        "consumer_pkg"
    ]
    path = write_distribution(tmp_path / "d", doc)
    assert "consumer_pkg -> library_pkg -> consumer_pkg" in " ".join(
        check_refs.check_file(path, False, {})
    )


def test_package_name_is_unique_within_distro(tmp_path):
    doc = distribution()
    doc["repositories"]["library"]["packages"] = {"consumer_pkg": {"tags": ["common-library"]}}
    path = write_distribution(tmp_path / "d", doc)
    assert "package names are unique per distro" in " ".join(check_refs.check_file(path, False, {}))


def test_sweep_clones_transitive_repository_and_tracks_ref(tmp_path):
    doc = distribution(target_dependencies=["library_pkg"])
    dist = tmp_path / "d"
    write_distribution(dist, doc)
    rows = sweep_matrix.build_matrix(dist, tmp_path / "state", "eager")
    consumer = next(row for row in rows if row["repo_name"] == "consumer")
    assert consumer["index_dependencies"] == {"consumer_pkg": ["library_pkg"]}
    assert consumer["dependency_packages"] == "library_pkg"
    assert consumer["dependency_repositories"] == [
        {
            "repo_name": "library",
            "repository": "https://example.org/library",
            "ref_kind": "branch",
            "ref_value": "v1",
        }
    ]
    assert {
        row["repo_name"] for row in sweep_matrix.build_matrix(dist, tmp_path / "state", "nightly")
    } == {
        "consumer",
        "library",
    }

    state_dir = tmp_path / "state" / "jazzy"
    state_dir.mkdir(parents=True)
    state = sweep_matrix.registered_state(
        doc["repositories"]["consumer"], sweep_matrix.dependency_context(doc, "consumer")
    )
    (state_dir / "consumer.json").write_text(json.dumps(state), encoding="utf-8")
    assert not any(
        row["repo_name"] == "consumer"
        for row in sweep_matrix.build_matrix(dist, tmp_path / "state", "eager")
    )

    doc["repositories"]["library"]["ref"]["value"] = "v2"
    write_distribution(dist, doc)
    assert any(
        row["repo_name"] == "consumer"
        for row in sweep_matrix.build_matrix(dist, tmp_path / "state", "eager")
    )


def test_pr_diff_rebuilds_consumer_when_dependency_ref_changes(tmp_path):
    base_doc = distribution(target_dependencies=["library_pkg"])
    head_doc = distribution(target_dependencies=["library_pkg"], dependency_ref="v2")
    base = tmp_path / "base"
    head = tmp_path / "head"
    write_distribution(base, base_doc)
    write_distribution(head, head_doc)
    assert {row["repo_name"] for row in diff_matrix.build_matrix(base, head)} == {
        "consumer",
        "library",
    }
