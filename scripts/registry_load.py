#!/usr/bin/env python3
"""Shared, version-gated loader for distributions/<distro>.yaml.

Every reader of the registry format — the sweep matrix builder, the semantic
checks, the metadata backfill, and the site exporter — goes through this
module instead of raw ``yaml.safe_load``, so an unsupported ``schema_version``
is a HARD, uniform failure everywhere. Before this gate existed,
``schema_version`` was written but read by nothing: a format change would have
flipped every reader into a silent no-op (empty site, empty sweep matrices)
with green CI.

Format (schema_version "2", see schema/distribution.schema.json):

    schema_version: "2"
    ros_distro: jazzy
    repositories:
      <repo_name>:                 # registry-unique key
        url: <git url>             # canonical-unique per distro file
        ref: {kind: tag|sha|branch, value: "..."}   # ONE ref per repository
        governance: community | foundation
        maintainers: [...]         # repo-level default
        packages:                  # >= 1 registered package hosted by the repo
          <package_name>:
            tags: [...]            # required
            description: "..."     # optional card-description override
            maintainers: [...]     # optional per-package override

One repository = one ref: every package registered from a repository is
validated and distributed at that single ref (lockstep). Ref skew between
sibling packages is unrepresentable by construction.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

SUPPORTED_SCHEMA_VERSION = "2"


class RegistryError(Exception):
    """A distribution file cannot be loaded under the supported contract."""


def load_distribution(path: Path) -> dict:
    """Parse one distributions/<distro>.yaml, gating on schema_version.

    Raises RegistryError on YAML errors, non-mapping documents, an
    unsupported schema_version, or a missing/non-mapping `repositories` key.
    Never returns a partially-usable document for an unsupported version —
    silent empty output is exactly the failure mode this loader exists to kill.
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RegistryError(f"{path}: cannot parse: {exc}") from exc

    if not isinstance(doc, dict):
        raise RegistryError(f"{path}: expected a YAML mapping, got {type(doc).__name__}")

    version = doc.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise RegistryError(
            f"{path}: schema_version {version!r} is not supported by this tooling "
            f"(supported: {SUPPORTED_SCHEMA_VERSION!r}); please upgrade"
        )

    repositories = doc.get("repositories")
    if not isinstance(repositories, dict):
        raise RegistryError(f"{path}: `repositories` must be a mapping of repo entries")

    for repo_name, spec in repositories.items():
        ref = (spec or {}).get("ref")
        if ref is not None:
            value = (ref or {}).get("value") if isinstance(ref, dict) else None
            if value is not None and not isinstance(value, str):
                # YAML happily types `value: 1.20` as a float and str() would
                # silently mangle it to "1.2" — the sweep would then check out
                # the wrong ref. The schema requires a string; enforce it at
                # the uniform gate too (defense for paths that skip CI).
                raise RegistryError(
                    f"{path}::{repo_name}: ref value {value!r} must be a string "
                    f"(quote it in YAML)"
                )

    return doc


def load_distributions_dir(distributions_dir: Path) -> list[tuple[Path, dict]]:
    """Load every *.yaml in a distributions dir, sorted by filename."""
    return [
        (path, load_distribution(path))
        for path in sorted(distributions_dir.glob("*.yaml"))
    ]


def canonical_url(url: str) -> str:
    """Normalize a git remote URL for duplicate detection.

    Folds the spellings that point at the same repository — scheme, ssh
    `git@host:org/repo` scp form, userinfo, an explicit port, a trailing
    slash, a `.git` suffix, and case — into one canonical `host/path` string.
    Used to reject two repository entries that register the same repo under
    different spellings. (Ports are dropped deliberately: a host serving the
    same path on two ports is rarer than the same repo spelled with and
    without its standard port.)
    """
    u = url.strip()
    scp = re.match(r"^(?:[^@/]+@)?([^:/@]+):(?!//)(.+)$", u)
    if "://" in u:
        parsed = urlsplit(u)
        host = parsed.hostname or ""
        path = parsed.path
    elif scp:
        # scp-like syntax: [user@]host:path
        host, path = scp.group(1), scp.group(2)
    else:
        host, path = "", u
    combined = f"{host}/{path.lstrip('/')}" if host else path
    combined = combined.rstrip("/")
    combined = combined.removesuffix(".git")
    return combined.lower()


def flatten_packages(doc: dict, *, distro: str | None = None) -> list[dict]:
    """Flatten a v2 document into one record per registered package.

    Each record carries the repository context the package inherits:

        {distro, package, repo_name, repository, ref, governance,
         tags, maintainers, description}

    `maintainers` resolves the per-package override over the repo-level
    default. The package -> repository mapping is COMPUTED here at load time,
    never stored anywhere downstream (rosdistro's inverse-index pattern):
    history/, metadata/, and the site keep their per-package keying.
    """
    distro = distro or doc.get("ros_distro", "")
    records: list[dict] = []
    for repo_name, spec in (doc.get("repositories") or {}).items():
        spec = spec or {}
        repo_maintainers = spec.get("maintainers") or []
        for package, pkg_spec in (spec.get("packages") or {}).items():
            pkg_spec = pkg_spec or {}
            records.append(
                {
                    "distro": distro,
                    "package": package,
                    "repo_name": repo_name,
                    "repository": spec.get("url", ""),
                    "ref": spec.get("ref") or {},
                    "governance": spec.get("governance", "community"),
                    "tags": pkg_spec.get("tags") or [],
                    "maintainers": pkg_spec.get("maintainers") or repo_maintainers,
                    "description": pkg_spec.get("description") or "",
                }
            )
    return records
