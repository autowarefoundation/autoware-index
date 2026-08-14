#!/usr/bin/env python3
"""Shared, version-gated loader for distributions/<distro>.yaml.

Every reader of the registry format (the sweep matrix builder, the semantic
checks, the metadata backfill, and the site exporter) goes through this
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
            tags: [...]            # required; live ids from schema/tags.yaml
            description: "..."     # optional card-description override
            maintainers: [...]     # optional per-package override

One repository = one ref: every package registered from a repository is
validated and distributed at that single ref (lockstep). Ref skew between
sibling packages is unrepresentable by construction.
"""

from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

SUPPORTED_SCHEMA_VERSION = "2"

TAG_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
TAG_ID_MAX_LENGTH = 20

_TAG_KEYS = {"group", "summary", "disambiguation", "label", "aliases"}
_DEPRECATED_KEYS = {"replaced_by", "note"}
_RESERVED_KEYS = {"note", "see"}


class RegistryError(Exception):
    """A distribution file cannot be loaded under the supported contract."""


def load_distribution(path: Path) -> dict:
    """Parse one distributions/<distro>.yaml, gating on schema_version.

    Raises RegistryError on YAML errors, non-mapping documents, an
    unsupported schema_version, or a missing/non-mapping `repositories` key.
    Never returns a partially-usable document for an unsupported version:
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
                # silently mangle it to "1.2"; the sweep would then check out
                # the wrong ref. The schema requires a string; enforce it at
                # the uniform gate too (defense for paths that skip CI).
                raise RegistryError(
                    f"{path}::{repo_name}: ref value {value!r} must be a string "
                    f"(quote it in YAML)"
                )

    return doc


def load_distributions_dir(distributions_dir: Path) -> list[tuple[Path, dict]]:
    """Load every *.yaml in a distributions dir, sorted by filename."""
    return [(path, load_distribution(path)) for path in sorted(distributions_dir.glob("*.yaml"))]


def _check_tag_id(path: Path, tag_id: object) -> None:
    if (
        not isinstance(tag_id, str)
        or not TAG_ID_PATTERN.match(tag_id)
        or len(tag_id) > TAG_ID_MAX_LENGTH
    ):
        raise RegistryError(
            f"{path}::{tag_id}: tag id must match {TAG_ID_PATTERN.pattern} "
            f"and be at most {TAG_ID_MAX_LENGTH} characters"
        )


def load_vocabulary(path: Path) -> dict:
    """Parse and self-check schema/tags.yaml (the closed tag vocabulary).

    Returns ``{"groups": {...}, "tags": {...}, "deprecated": {...},
    "aliases": {...}, "reserved": {...}}`` with ``deprecated`` and
    ``reserved`` defaulting to empty mappings and ``aliases`` computed as one
    flat ``alias -> canonical live id`` map from the per-tag ``aliases:``
    lists. Raises RegistryError on the first
    inconsistency: unparseable/non-mapping document, a malformed tag id, an
    unknown key in a tag spec (catches ``sumary:``-style typos), a missing
    or empty ``summary``, a ``group`` not declared under ``groups:``, an id
    that is both live and deprecated, a ``replaced_by`` target that is not
    a live tag (deprecation chains are rejected; always point at the final
    replacement), a malformed ``label``/``aliases`` value, or an alias that
    collides with a live id, a deprecated id, or another tag's alias (the
    four namespaces stay pairwise disjoint). Every reader (check_tags.py,
    site/build.py) goes through this gate, so a broken vocabulary is a hard,
    uniform failure, never a vacuously-passing check or a silently ungrouped
    site.
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RegistryError(f"{path}: cannot parse: {exc}") from exc

    if not isinstance(doc, dict):
        raise RegistryError(f"{path}: expected a YAML mapping, got {type(doc).__name__}")

    groups = doc.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise RegistryError(f"{path}: `groups` must be a non-empty mapping of group id -> title")
    for group_id, title in groups.items():
        if not isinstance(title, str) or not title.strip():
            raise RegistryError(f"{path}: group {group_id!r} title must be a non-empty string")

    tags = doc.get("tags")
    if not isinstance(tags, dict) or not tags:
        raise RegistryError(f"{path}: `tags` must be a non-empty mapping of tag id -> spec")
    for tag_id, spec in tags.items():
        _check_tag_id(path, tag_id)
        if not isinstance(spec, dict):
            raise RegistryError(f"{path}::{tag_id}: tag spec must be a mapping")
        unknown = set(spec) - _TAG_KEYS
        if unknown:
            raise RegistryError(
                f"{path}::{tag_id}: unknown key(s) {sorted(unknown)!r} "
                f"(allowed: {sorted(_TAG_KEYS)!r})"
            )
        group = spec.get("group")
        if group not in groups:
            raise RegistryError(
                f"{path}::{tag_id}: `group` {group!r} is not defined under `groups`"
            )
        summary = spec.get("summary")
        if not isinstance(summary, str) or not summary.strip() or "\n" in summary.strip():
            raise RegistryError(
                f"{path}::{tag_id}: `summary` must be a non-empty single-line string"
            )
        disambiguation = spec.get("disambiguation")
        if disambiguation is not None and (
            not isinstance(disambiguation, str) or not disambiguation.strip()
        ):
            raise RegistryError(f"{path}::{tag_id}: `disambiguation` must be a non-empty string")
        label = spec.get("label")
        if label is not None and (
            not isinstance(label, str) or not label.strip() or "\n" in label.strip()
        ):
            raise RegistryError(f"{path}::{tag_id}: `label` must be a non-empty single-line string")
        aliases = spec.get("aliases")
        if aliases is not None:
            if not isinstance(aliases, list) or not aliases:
                raise RegistryError(
                    f"{path}::{tag_id}: `aliases` must be a non-empty list of alias strings"
                )
            for alias in aliases:
                # Aliases share the id grammar so search stays predictable,
                # but not the length cap: they are search terms, never ids,
                # and never valid in distributions/*.yaml.
                if not isinstance(alias, str) or not TAG_ID_PATTERN.match(alias):
                    raise RegistryError(
                        f"{path}::{tag_id}: alias {alias!r} must match {TAG_ID_PATTERN.pattern}"
                    )

    deprecated = doc.get("deprecated") or {}
    if not isinstance(deprecated, dict):
        raise RegistryError(f"{path}: `deprecated` must be a mapping of tag id -> spec")
    for tag_id, spec in deprecated.items():
        _check_tag_id(path, tag_id)
        if tag_id in tags:
            raise RegistryError(f"{path}::{tag_id}: deprecated id is also a live tag")
        if not isinstance(spec, dict):
            raise RegistryError(f"{path}::{tag_id}: deprecated spec must be a mapping")
        unknown = set(spec) - _DEPRECATED_KEYS
        if unknown:
            raise RegistryError(
                f"{path}::{tag_id}: unknown key(s) {sorted(unknown)!r} "
                f"(allowed: {sorted(_DEPRECATED_KEYS)!r})"
            )
        replaced_by = spec.get("replaced_by")
        if not isinstance(replaced_by, list) or not all(isinstance(t, str) for t in replaced_by):
            raise RegistryError(
                f"{path}::{tag_id}: `replaced_by` must be a list of live tag ids "
                f"(may be empty for retirement without replacement)"
            )
        for target in replaced_by:
            if target not in tags:
                raise RegistryError(
                    f"{path}::{tag_id}: `replaced_by` target {target!r} is not a live tag"
                )
        note = spec.get("note")
        if note is not None and (not isinstance(note, str) or not note.strip()):
            raise RegistryError(f"{path}::{tag_id}: `note` must be a non-empty string")

    # Flatten per-tag aliases into one alias -> canonical map, enforcing that
    # live ids, deprecated ids, and aliases never overlap: an alias that
    # shadows an id would make check_tags' diagnostics ambiguous.
    aliases_map: dict[str, str] = {}
    for tag_id, spec in tags.items():
        for alias in spec.get("aliases") or []:
            if alias in tags:
                raise RegistryError(f"{path}::{tag_id}: alias {alias!r} is also a live tag id")
            if alias in deprecated:
                raise RegistryError(f"{path}::{tag_id}: alias {alias!r} is also a deprecated id")
            if alias in aliases_map:
                raise RegistryError(
                    f"{path}::{tag_id}: alias {alias!r} is already an alias of "
                    f"{aliases_map[alias]!r}"
                )
            aliases_map[alias] = tag_id

    # Reserved ids: decided never to mint, with the recorded refusal printed
    # by check_tags at the moment of the mistake. Disjoint from every other
    # namespace; `see:` targets must be live so the pointer never dangles.
    reserved = doc.get("reserved") or {}
    if not isinstance(reserved, dict):
        raise RegistryError(f"{path}: `reserved` must be a mapping of tag id -> spec")
    for tag_id, spec in reserved.items():
        _check_tag_id(path, tag_id)
        if tag_id in tags:
            raise RegistryError(f"{path}::{tag_id}: reserved id is also a live tag")
        if tag_id in deprecated:
            raise RegistryError(f"{path}::{tag_id}: reserved id is also a deprecated id")
        if tag_id in aliases_map:
            raise RegistryError(
                f"{path}::{tag_id}: reserved id is also an alias of {aliases_map[tag_id]!r}"
            )
        if not isinstance(spec, dict):
            raise RegistryError(f"{path}::{tag_id}: reserved spec must be a mapping")
        unknown = set(spec) - _RESERVED_KEYS
        if unknown:
            raise RegistryError(
                f"{path}::{tag_id}: unknown key(s) {sorted(unknown)!r} "
                f"(allowed: {sorted(_RESERVED_KEYS)!r})"
            )
        note = spec.get("note")
        if not isinstance(note, str) or not note.strip():
            raise RegistryError(
                f"{path}::{tag_id}: reserved `note` must be a non-empty string "
                f"(it is the refusal check_tags prints)"
            )
        see = spec.get("see")
        if see is not None:
            if not isinstance(see, list) or not all(isinstance(t, str) for t in see):
                raise RegistryError(f"{path}::{tag_id}: `see` must be a list of live tag ids")
            for target in see:
                if target not in tags:
                    raise RegistryError(
                        f"{path}::{tag_id}: `see` target {target!r} is not a live tag"
                    )

    return {
        "groups": groups,
        "tags": tags,
        "deprecated": deprecated,
        "aliases": aliases_map,
        "reserved": reserved,
    }


def canonical_url(url: str) -> str:
    """Normalize a git remote URL for duplicate detection.

    Folds the spellings that point at the same repository (scheme, ssh
    `git@host:org/repo` scp form, userinfo, an explicit port, a trailing
    slash, a `.git` suffix, and case) into one canonical `host/path` string.
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
         reference_design, tags, maintainers, description}

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
                    "reference_design": spec.get("reference_design") or [],
                    "tags": pkg_spec.get("tags") or [],
                    "maintainers": pkg_spec.get("maintainers") or repo_maintainers,
                    "description": pkg_spec.get("description") or "",
                }
            )
    return records
