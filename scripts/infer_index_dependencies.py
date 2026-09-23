"""Derive Index source dependencies from package.xml at a registered git ref."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

from catkin_pkg.condition import evaluate_condition

# These are concrete package dependencies. group_depend names a package group,
# rather than an individual package, and cannot identify an Index entry.
DEPENDENCY_TAGS = {
    "depend",
    "build_depend",
    "build_export_depend",
    "buildtool_depend",
    "buildtool_export_depend",
    "exec_depend",
    "test_depend",
    "doc_depend",
}
CONDITION_VARIABLE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


class DependencyInferenceError(Exception):
    """A source manifest cannot be used to derive this registration."""


def condition_applies(condition: str | None, context: dict[str, str]) -> bool:
    """Keep unknown-variable dependencies rather than silently omit sources."""
    result = evaluate_condition(condition, context)
    return bool(set(CONDITION_VARIABLE.findall(condition or "")) - context.keys()) or result


def read_package_manifests(root: Path, distro: str) -> dict[str, set[str]]:
    """Read tracked package.xml files, rejecting ambiguous package names."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*package.xml"],
        capture_output=True,
        check=True,
    )
    manifests: dict[str, set[str]] = {}
    for raw_path in filter(None, result.stdout.split(b"\0")):
        path = root / raw_path.decode("utf-8")
        if path.name != "package.xml":
            continue
        try:
            document = ET.parse(path)
        except (ET.ParseError, OSError) as exc:
            raise DependencyInferenceError(f"cannot read {path.relative_to(root)}: {exc}") from exc
        package = document.getroot()
        name = (package.findtext("name") or "").strip() if package.tag == "package" else ""
        if not name:
            raise DependencyInferenceError(f"{path.relative_to(root)} has no package name")
        if name in manifests:
            raise DependencyInferenceError(f"more than one package.xml declares {name!r}")
        context = {"ROS_DISTRO": distro, "ROS_VERSION": "2", "ROS_PYTHON_VERSION": "3"}
        try:
            manifests[name] = {
                (element.text or "").strip()
                for element in package
                if element.tag in DEPENDENCY_TAGS
                and (element.text or "").strip()
                and condition_applies(element.get("condition"), context)
            }
        except ValueError as exc:
            raise DependencyInferenceError(
                f"invalid dependency condition in {path.relative_to(root)}: {exc}"
            ) from exc
    return manifests


def infer_dependencies(
    spec: dict, registered_names: set[str], manifests: dict[str, set[str]]
) -> None:
    """Replace submitted edges with package.xml matches in the same distro."""
    packages = spec.get("packages") or {}
    index_names = registered_names | set(packages)
    for name, package_spec in packages.items():
        if name not in manifests:
            raise DependencyInferenceError(
                f"registered package {name!r} has no package.xml at this ref"
            )
        dependencies = manifests[name] & index_names
        if name in dependencies:
            raise DependencyInferenceError(f"package {name!r} depends on itself in package.xml")
        if dependencies:
            package_spec["index_dependencies"] = sorted(dependencies)
        else:
            package_spec.pop("index_dependencies", None)


def infer_from_repository(spec: dict, registered_names: set[str], distro: str) -> None:
    """Clone the submitted ref and derive edges without executing repository code."""
    url = spec.get("url") or ""
    ref_spec = spec.get("ref") or {}
    ref = ref_spec.get("value") or ""
    if not url or not ref:
        raise DependencyInferenceError("repository URL and ref are required to infer dependencies")
    checkout_ref = {
        "branch": f"refs/remotes/origin/{ref}",
        "tag": f"refs/tags/{ref}",
        "sha": ref,
    }.get(ref_spec.get("kind"))
    if checkout_ref is None:
        raise DependencyInferenceError(f"unsupported ref kind {ref_spec.get('kind')!r}")
    with TemporaryDirectory(prefix="index-registration-") as directory:
        root = Path(directory) / "source"
        try:
            subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", "--", url, str(root)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "checkout", "--quiet", "--detach", checkout_ref],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise DependencyInferenceError(
                f"cannot read repository at {ref!r}: {(exc.stderr or '').strip()}"
            ) from exc
        infer_dependencies(spec, registered_names, read_package_manifests(root, distro))
