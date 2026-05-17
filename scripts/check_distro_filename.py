#!/usr/bin/env python3
"""Assert each distributions/<distro>.yaml has ros_distro matching the filename stem.

Mirrors the consistency check in .github/workflows/validate.yaml so failures
surface locally via pre-commit before they ever reach CI.
"""

from __future__ import annotations

import pathlib
import sys

import yaml


def check(paths: list[str]) -> int:
    failed = False
    for arg in paths:
        path = pathlib.Path(arg)
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            print(f"{path}: YAML parse error: {exc}", file=sys.stderr)
            failed = True
            continue
        declared = data.get("ros_distro") if isinstance(data, dict) else None
        if declared != path.stem:
            print(
                f"{path}: ros_distro is {declared!r} but filename stem is {path.stem!r}",
                file=sys.stderr,
            )
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(check(sys.argv[1:]))
