#!/usr/bin/env python3
"""Emit a sweep matrix of every `kind: branch` package for the nightly sweep.

Pinned `tag`/`sha` refs are immutable: once the eager sweep validated them
against the current latest Autoware release, re-testing the same commit adds
nothing (a new Autoware release is picked up by the next eager sweep when the
ref changes, or — for branch refs — here). So the nightly sweep re-resolves and
re-tests only `kind: branch` entries, whose tips move underneath a fixed ref
value.

Output matches sweep_eager_matrix.py exactly (same row shape, same
`{"include": [...]}` envelope) so both feed the identical
validate -> record pipeline:

    [
      {
        "ros_distro": "jazzy",
        "package_name": "autoware_livox_tag_filter",
        "package_repository": "https://github.com/.../autoware_livox_tag_filter",
        "ref_kind": "branch",
        "ref_value": "main"
      },
      ...
    ]

Usage:
    scripts/sweep_nightly_matrix.py [--distributions-dir distributions] [--output -]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


def build_matrix(distributions_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(distributions_dir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        ros_distro = doc.get("ros_distro")
        for name, spec in (doc.get("packages") or {}).items():
            spec = spec or {}
            ref = spec.get("ref") or {}
            if ref.get("kind") != "branch":
                continue
            repository = spec.get("repository")
            ref_value = ref.get("value")
            if not (ros_distro and repository and ref_value):
                print(
                    f"skipping {path}::{name}: missing fields for a branch ref",
                    file=sys.stderr,
                )
                continue
            rows.append(
                {
                    "ros_distro": ros_distro,
                    "package_name": name,
                    "package_repository": repository,
                    "ref_kind": "branch",
                    "ref_value": ref_value,
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distributions-dir", default="distributions")
    parser.add_argument("--output", default="-", help="Output path; '-' for stdout")
    args = parser.parse_args()

    rows = build_matrix(Path(args.distributions_dir))
    payload = json.dumps({"include": rows}, separators=(",", ":"))

    if args.output == "-":
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
