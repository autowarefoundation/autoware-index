#!/usr/bin/env python3
"""Tag-vocabulary check for distributions/<distro>.yaml.

Every `tags:` value of every registered package must be a LIVE id in
schema/tags.yaml — the closed vocabulary the browse site's filters and the
aw-index-cli `--tags` selection (its own repo) are built on. Two classes of
defect beyond JSON-schema shape (which only checks count/pattern/uniqueness):

  1. Unknown tag. A typo (`sensign`) or an unproposed concept (`slam`).
     Rejected with a nearest-match suggestion; new tags are proposed by a PR
     editing schema/tags.yaml (see CONTRIBUTING.md).

  2. Deprecated tag. A retired id may never (re)enter the registry; the error
     names its replacement. Because this is a hard error and vocabulary edits
     re-run this check over every distro file, a PR deprecating a tag cannot
     merge without migrating every usage in the same commit — deprecation and
     migration are atomic by construction.

The vocabulary itself is self-checked by registry_load.load_vocabulary (bad
ids, unknown groups, replaced_by chains, ...); a broken vocabulary is a hard
failure here, never a vacuous pass.

One non-blocking nudge: `tool` as a package's ONLY tag emits a ::warning::
annotation (add the domain the tool serves) without failing the check.

The check is fully offline. Tags stay OUT of the sweep diff tuple and the
history records: re-tagging is a metadata-only change that never burns a
sweep, and this script imports nothing from the sweep modules.

Usage:
    scripts/check_tags.py [distributions/*.yaml] [--vocabulary schema/tags.yaml]

With no positional paths it checks every distributions/*.yaml — that is what
the pass_filenames:false pre-commit hook runs, so editing EITHER a distro
file or the vocabulary re-checks the whole registry.
"""

from __future__ import annotations

import argparse
import difflib
from pathlib import Path
import sys

from registry_load import RegistryError
from registry_load import flatten_packages
from registry_load import load_distribution
from registry_load import load_vocabulary

DEFAULT_VOCABULARY = Path("schema") / "tags.yaml"
DEFAULT_DISTRIBUTIONS_GLOB = (Path("distributions"), "*.yaml")


def suggest(tag: str, live: set[str]) -> str | None:
    """Nearest live tag id for a did-you-mean hint, or None if nothing is close."""
    matches = difflib.get_close_matches(tag, sorted(live), n=1, cutoff=0.6)
    return matches[0] if matches else None


def check_package_tags(file: str, owner: str, tags: list, vocabulary: dict) -> list[str]:
    """Errors for one package's tag list against the loaded vocabulary."""
    errors: list[str] = []
    live = set(vocabulary["tags"])
    deprecated = vocabulary["deprecated"]
    for tag in tags or []:
        if not isinstance(tag, str):
            # Shape defense for files that skip CI (same idiom as the
            # non-string ref.value guard in registry_load): a nested list or
            # mapping here must be a diagnostic, not a TypeError traceback.
            errors.append(f"{file}::{owner}: tag {tag!r} must be a string")
            continue
        if tag in live:
            continue
        if tag in deprecated:
            replacements = deprecated[tag].get("replaced_by") or []
            if replacements:
                errors.append(
                    f"{file}::{owner}: tag {tag!r} is deprecated; use: "
                    f"{', '.join(replacements)}"
                )
            else:
                errors.append(
                    f"{file}::{owner}: tag {tag!r} is deprecated "
                    f"(retired without replacement; see schema/tags.yaml)"
                )
            continue
        near = suggest(str(tag), live)
        if near:
            errors.append(f"{file}::{owner}: unknown tag {tag!r} (did you mean: {near}?)")
        else:
            errors.append(
                f"{file}::{owner}: unknown tag {tag!r} (no close match; see schema/tags.yaml)"
            )
    return errors


def check_file(path: Path, vocabulary: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for one distribution file."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        doc = load_distribution(path)
    except RegistryError as exc:
        return [str(exc)], []

    for record in flatten_packages(doc):
        owner = f"{record['repo_name']}.{record['package']}"
        tags = record["tags"]
        if not isinstance(tags, list):
            # `tags: sensing` (a bare string would iterate per character) or
            # `tags: 5` — reject the shape instead of misdiagnosing items.
            errors.append(
                f"{path}::{owner}: `tags` must be a list of tag ids, " f"got {type(tags).__name__}"
            )
            continue
        errors.extend(check_package_tags(str(path), owner, tags, vocabulary))
        if tags == ["tool"]:
            warnings.append(
                f"::warning::{path}::{owner}: 'tool' is the only tag — "
                f"add the domain it serves (e.g. calibration, map, planning)"
            )
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="distributions/<distro>.yaml files (default: distributions/*.yaml)",
    )
    parser.add_argument(
        "--vocabulary",
        type=Path,
        default=DEFAULT_VOCABULARY,
        help="tag vocabulary file (default: %(default)s)",
    )
    args = parser.parse_args()

    try:
        vocabulary = load_vocabulary(args.vocabulary)
    except RegistryError as exc:
        print(str(exc), file=sys.stderr)
        print("check_tags: 1 problem(s) found", file=sys.stderr)
        return 1

    directory, pattern = DEFAULT_DISTRIBUTIONS_GLOB
    paths = [Path(p) for p in args.paths] or sorted(directory.glob(pattern))
    if not paths:
        # An empty glob (wrong cwd, renamed dir) must never be a vacuous pass.
        print(
            f"check_tags: no distribution files found under {directory}/ "
            f"(run from the repo root or pass paths)",
            file=sys.stderr,
        )
        return 1

    errors: list[str] = []
    warnings: list[str] = []
    for path in paths:
        file_errors, file_warnings = check_file(path, vocabulary)
        errors.extend(file_errors)
        warnings.extend(file_warnings)

    for w in warnings:
        print(w, file=sys.stderr)
    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        print(f"check_tags: {len(errors)} problem(s) found", file=sys.stderr)
        return 1
    print(
        f"check_tags: all package tags are in the vocabulary "
        f"({len(vocabulary['tags'])} live, {len(vocabulary['deprecated'])} deprecated)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
