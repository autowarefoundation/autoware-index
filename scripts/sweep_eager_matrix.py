#!/usr/bin/env python3
"""Detect packages whose ref changed between two commits and emit a sweep matrix.

Diffs `distributions/*.yaml` between BEFORE_SHA and AFTER_SHA, finds entries
under `packages.<name>.ref` that differ, and emits a JSON matrix of
validation rows for the eager sweep workflow:

    [
      {
        "ros_distro": "jazzy",
        "package_name": "autoware_livox_tag_filter",
        "package_repository": "https://github.com/.../autoware_livox_tag_filter",
        "ref_kind": "tag",
        "ref_value": "0.2.1"
      },
      ...
    ]

One matrix row per changed package. The workflow dispatches one
sweep-package job per row; the sweep reusable resolves the Autoware
version at runtime via the latest-autoware-version composite action.

When BEFORE_SHA is the all-zero sentinel (branch was just created) we treat
every package in every distribution file as "changed" — first push to main
fans out a full validation.

Usage:
    scripts/sweep_eager_matrix.py \\
        --before <sha-or-zeros> --after <sha> \\
        [--output matrix.json]

Without --output, prints to stdout. Used in CI via:
    matrix=$(scripts/sweep_eager_matrix.py --before "$BEFORE" --after "$AFTER")
    echo "matrix=$matrix" >> "$GITHUB_OUTPUT"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

ZERO_SHA = "0000000000000000000000000000000000000000"
DISTRIBUTIONS_DIR = Path("distributions")


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def load_yaml_at(sha: str, path: str) -> dict | None:
    """Return parsed YAML at <sha>:<path>, or None if it didn't exist."""
    result = subprocess.run(
        ["git", "show", f"{sha}:{path}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    return yaml.safe_load(result.stdout)


def list_distribution_files(sha: str) -> list[str]:
    result = run(["git", "ls-tree", "-r", "--name-only", sha, "distributions/"])
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().endswith(".yaml")
    ]


def packages_with_changed_ref(before_sha: str, after_sha: str) -> list[tuple[str, str]]:
    """List (file, package_name) whose ref changed between the two shas.

    On initial commit (before_sha is zeros), every package is "changed".
    """
    after_files = list_distribution_files(after_sha)
    changed: list[tuple[str, str]] = []

    for f in after_files:
        after_doc = load_yaml_at(after_sha, f) or {}
        after_pkgs = after_doc.get("packages") or {}

        if before_sha == ZERO_SHA:
            changed.extend((f, name) for name in after_pkgs)
            continue

        before_doc = load_yaml_at(before_sha, f) or {}
        before_pkgs = before_doc.get("packages") or {}

        for name, after_spec in after_pkgs.items():
            before_spec = before_pkgs.get(name)
            after_ref = (after_spec or {}).get("ref")
            before_ref = (before_spec or {}).get("ref") if before_spec else None
            if after_ref != before_ref:
                changed.append((f, name))

    return changed


def build_matrix(before_sha: str, after_sha: str) -> list[dict]:
    rows: list[dict] = []
    for distro_file, package_name in packages_with_changed_ref(before_sha, after_sha):
        doc = load_yaml_at(after_sha, distro_file) or {}
        ros_distro = doc.get("ros_distro")
        package = (doc.get("packages") or {}).get(package_name) or {}
        repository = package.get("repository")
        ref = package.get("ref") or {}
        ref_kind = ref.get("kind")
        ref_value = ref.get("value")

        if not (ros_distro and repository and ref_kind and ref_value):
            print(
                f"skipping {distro_file}::{package_name}: missing fields after diff",
                file=sys.stderr,
            )
            continue

        rows.append(
            {
                "ros_distro": ros_distro,
                "package_name": package_name,
                "package_repository": repository,
                "ref_kind": ref_kind,
                "ref_value": ref_value,
            }
        )

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, help="Pre-change commit sha (40 hex zeros = branch creation)")
    parser.add_argument("--after", required=True, help="Post-change commit sha")
    parser.add_argument("--output", default="-", help="Output path; '-' for stdout")
    args = parser.parse_args()

    rows = build_matrix(args.before, args.after)
    payload = json.dumps({"include": rows}, separators=(",", ":"))

    if args.output == "-":
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
