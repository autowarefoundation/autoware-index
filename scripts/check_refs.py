#!/usr/bin/env python3
"""Semantic checks for distributions/<distro>.yaml beyond JSON-schema shape.

Four classes of defect the schema cannot catch:

  1. Unresolvable ref. A repository registered with a ref that does not exist
     (e.g. tag "0.2.1" on a repo with no tags). The sweep does a literal
     `git checkout <ref>`, so an unresolvable ref turns the first sweep into a
     hard failure. We verify resolvability with `git ls-remote` — once per
     repository entry (and memoized across distro files), not per package.

  2. Placeholder maintainers. `name: TBD`, `github: TBD`, or an `@example.com`
     email all satisfy the schema's `format: email` / `minLength` constraints
     but mean the entry is not production-real. Checked on the repository's
     default maintainers AND on every per-package override.

  3. Duplicate repository URL. Two repository entries whose URLs are spelling
     variants of the same repo (.git suffix, trailing slash, ssh vs https,
     case) would be swept twice and would defeat the CLI's one-entry-per-repo
     contract. Rejected via canonical-URL comparison.

  4. Duplicate package name. history/<distro>/<package>.ndjson,
     metadata/<distro>/<package>.xml, and the site join all key on the package
     name, so a package name may appear in exactly ONE repository entry per
     distro file (rosdistro's `_add_package` invariant).

Resolvability needs network access, so it is opt-out via --no-network (used by
the pre-commit mirror, which runs the offline checks only). CI runs the full
check.

  ref kind | resolvability rule
  ---------|-------------------------------------------------------------
  branch   | `git ls-remote --heads <repo> <value>` returns refs/heads/<value>
  tag      | `git ls-remote --tags  <repo> <value>` returns refs/tags/<value>
  sha      | 40 lowercase hex chars (reachability is not probed — ls-remote
           |   cannot list an arbitrary commit; the sweep's checkout is the
           |   backstop)

Usage:
    scripts/check_refs.py distributions/*.yaml
    scripts/check_refs.py --no-network distributions/*.yaml   # offline checks only
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from registry_load import RegistryError, canonical_url, load_distribution

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


def check_maintainers(file: str, owner: str, maintainers: list[dict]) -> list[str]:
    errors: list[str] = []
    for m in maintainers or []:
        name = str(m.get("name", "")).strip()
        github = str(m.get("github", "")).strip()
        email = str(m.get("email", "")).strip().lower()
        if name.lower() in PLACEHOLDER_NAMES:
            errors.append(f"{file}::{owner}: placeholder maintainer name {name!r}")
        if github.lower() in PLACEHOLDER_NAMES:
            errors.append(f"{file}::{owner}: placeholder maintainer github {github!r}")
        if email.endswith("@example.com") or email.endswith("@example.org"):
            errors.append(f"{file}::{owner}: placeholder maintainer email {email!r}")
    return errors


def check_ref(
    file: str,
    repo_name: str,
    url: str,
    ref: dict,
    network: bool,
    resolve_cache: dict[tuple[str, str, str], bool],
) -> list[str]:
    kind = ref.get("kind")
    value = str(ref.get("value", ""))

    if kind == "sha":
        if not SHA_RE.match(value):
            return [f"{file}::{repo_name}: sha ref {value!r} is not 40 lowercase hex chars"]
        return []

    if not network:
        return []

    key = (url, kind or "", value)
    if key not in resolve_cache:
        ref_filter = "--heads" if kind == "branch" else "--tags"
        resolve_cache[key] = ls_remote(url, ref_filter, value)
    if not resolve_cache[key]:
        return [
            f"{file}::{repo_name}: {kind} ref {value!r} does not resolve in {url} "
            f"(git ls-remote found no match)"
        ]
    return []


def check_file(path: Path, network: bool, resolve_cache: dict) -> list[str]:
    errors: list[str] = []
    try:
        doc = load_distribution(path)
    except RegistryError as exc:
        return [str(exc)]

    seen_urls: dict[str, str] = {}
    seen_packages: dict[str, str] = {}
    for repo_name, spec in (doc.get("repositories") or {}).items():
        spec = spec or {}
        url = spec.get("url") or ""

        if url:
            canon = canonical_url(url)
            if canon in seen_urls:
                errors.append(
                    f"{path}::{repo_name}: repository URL duplicates entry "
                    f"{seen_urls[canon]!r} ({canon}); one entry per repository"
                )
            else:
                seen_urls[canon] = repo_name

        errors.extend(check_maintainers(str(path), repo_name, spec.get("maintainers") or []))

        for package, pkg_spec in (spec.get("packages") or {}).items():
            pkg_spec = pkg_spec or {}
            if package in seen_packages:
                errors.append(
                    f"{path}::{repo_name}: package {package!r} is already registered by "
                    f"entry {seen_packages[package]!r}; package names are unique per distro"
                )
            else:
                seen_packages[package] = repo_name
            if pkg_spec.get("maintainers"):
                errors.extend(
                    check_maintainers(str(path), f"{repo_name}.{package}", pkg_spec["maintainers"])
                )

        ref = spec.get("ref") or {}
        if ref and url:
            errors.extend(check_ref(str(path), repo_name, url, ref, network, resolve_cache))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="distributions/<distro>.yaml files")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="skip ref-resolvability (git ls-remote); run the offline checks only",
    )
    args = parser.parse_args()

    errors: list[str] = []
    resolve_cache: dict[tuple[str, str, str], bool] = {}
    for arg in args.paths:
        errors.extend(check_file(Path(arg), network=not args.no_network, resolve_cache=resolve_cache))

    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        print(f"check_refs: {len(errors)} problem(s) found", file=sys.stderr)
        return 1
    print(
        "check_refs: refs resolve, URLs and package names unique, no placeholder maintainers",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
