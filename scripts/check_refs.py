#!/usr/bin/env python3
"""Semantic checks for distributions/<distro>.yaml beyond JSON-schema shape.

Two classes of defect the schema cannot catch, both of which shipped in the
seed entry and motivated this guard:

  1. Unresolvable ref. A package registered with a ref that does not exist in
     its repository (e.g. tag "0.2.1" on a repo with no tags). The eager sweep
     does a literal `git checkout <ref>`, so an unresolvable ref turns the first
     sweep into a hard failure. We verify resolvability with `git ls-remote`.

  2. Placeholder maintainers. `name: TBD`, `github: TBD`, or an `@example.com`
     email all satisfy the schema's `format: email` / `minLength` constraints
     but mean the entry is not production-real. We reject them outright.

Resolvability needs network access, so it is opt-out via --no-network (used by
the pre-commit mirror, which runs the placeholder check offline). CI runs the
full check.

  ref kind | resolvability rule
  ---------|-------------------------------------------------------------
  branch   | `git ls-remote --heads <repo> <value>` returns refs/heads/<value>
  tag      | `git ls-remote --tags  <repo> <value>` returns refs/tags/<value>
  sha      | 40 lowercase hex chars (reachability is not probed — ls-remote
           |   cannot list an arbitrary commit; the sweep's checkout is the
           |   backstop)

Usage:
    scripts/check_refs.py distributions/*.yaml
    scripts/check_refs.py --no-network distributions/*.yaml   # placeholders only
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PLACEHOLDER_NAMES = {"tbd", "todo", "n/a", "na", "none", "xxx", ""}


def ls_remote(repository: str, ref_filter: str, value: str) -> bool:
    """True if `git ls-remote <ref_filter> <repository> <value>` resolves."""
    result = subprocess.run(
        ["git", "ls-remote", ref_filter, repository, value],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def check_maintainers(file: str, package: str, maintainers: list[dict]) -> list[str]:
    errors: list[str] = []
    for m in maintainers or []:
        name = str(m.get("name", "")).strip()
        github = str(m.get("github", "")).strip()
        email = str(m.get("email", "")).strip().lower()
        if name.lower() in PLACEHOLDER_NAMES:
            errors.append(f"{file}::{package}: placeholder maintainer name {name!r}")
        if github.lower() in PLACEHOLDER_NAMES:
            errors.append(f"{file}::{package}: placeholder maintainer github {github!r}")
        if email.endswith("@example.com") or email.endswith("@example.org"):
            errors.append(f"{file}::{package}: placeholder maintainer email {email!r}")
    return errors


def check_ref(file: str, package: str, repository: str, ref: dict, network: bool) -> list[str]:
    kind = ref.get("kind")
    value = str(ref.get("value", ""))

    if kind == "sha":
        if not SHA_RE.match(value):
            return [f"{file}::{package}: sha ref {value!r} is not 40 lowercase hex chars"]
        return []

    if not network:
        return []

    if kind == "branch":
        if not ls_remote(repository, "--heads", value):
            return [
                f"{file}::{package}: branch ref {value!r} does not resolve in {repository} "
                f"(git ls-remote --heads found no match)"
            ]
    elif kind == "tag":
        if not ls_remote(repository, "--tags", value):
            return [
                f"{file}::{package}: tag ref {value!r} does not resolve in {repository} "
                f"(git ls-remote --tags found no match)"
            ]
    return []


def check_file(path: Path, network: bool) -> list[str]:
    errors: list[str] = []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        return [f"{path}: YAML parse error: {exc}"]

    packages = (data.get("packages") or {}) if isinstance(data, dict) else {}
    for name, spec in packages.items():
        spec = spec or {}
        errors.extend(check_maintainers(str(path), name, spec.get("maintainers") or []))
        ref = spec.get("ref") or {}
        repository = spec.get("repository") or ""
        if ref and repository:
            errors.extend(check_ref(str(path), name, repository, ref, network))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="distributions/<distro>.yaml files")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="skip ref-resolvability (git ls-remote); run placeholder checks only",
    )
    args = parser.parse_args()

    errors: list[str] = []
    for arg in args.paths:
        errors.extend(check_file(Path(arg), network=not args.no_network))

    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        print(f"check_refs: {len(errors)} problem(s) found", file=sys.stderr)
        return 1
    print("check_refs: all refs resolve and no placeholder maintainers", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
