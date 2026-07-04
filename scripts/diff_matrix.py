#!/usr/bin/env python3
"""Emit the PR build-check matrix: repository entries a PR adds or changes.

Diffs the HEAD registry (the PR's test-merge distributions/) against the BASE
registry (the same merge's base parent, checked out by build-check.yaml)
and emits one row per repository entry whose sweep tuple — (url, ref{kind,
value}, sorted registered package names), imported from sweep_matrix so the
two diffs can never drift — was added or changed by the PR. Metadata-only
edits (tags, descriptions, maintainers, governance) emit nothing, and entries
the PR does not touch are never blamed for being stale: that is what
distinguishes this diff from `sweep_matrix.py --mode eager`, which compares
EVERY entry against the data-branch state cursors.

Removed entries emit nothing (there is nothing left to build). Rows have the
exact shape sweep_matrix.py emits (consumed by sweep-repository.yaml in the
actions repo; `packages` is space-separated for workflow_call string inputs).

Usage:
    scripts/diff_matrix.py --base-dir _base/distributions --head-dir distributions
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from registry_load import RegistryError
from registry_load import load_distributions_dir
from sweep_matrix import MAX_ROWS_DEFAULT
from sweep_matrix import registered_state


def base_states(base_dir: Path) -> dict[tuple[str, str], dict]:
    """Map (distro, repo_name) -> sweep tuple for every BASE registry entry.

    Malformed base entries keep their (partial) tuple: a head entry that fixes
    one compares unequal and is rebuilt, which is the honest outcome.
    """
    states: dict[tuple[str, str], dict] = {}
    for path, doc in load_distributions_dir(base_dir):
        distro = doc.get("ros_distro") or path.stem
        for repo_name, spec in (doc.get("repositories") or {}).items():
            states[(distro, repo_name)] = registered_state(spec or {})
    return states


def build_matrix(base_dir: Path, head_dir: Path) -> list[dict]:
    if not base_dir.is_dir():
        # A missing base dir would silently classify every entry as "added"
        # and build the whole registry — that is always a miswired checkout
        # path in the workflow, never a real registry state.
        raise RegistryError(f"base distributions dir not found: {base_dir}")

    base = base_states(base_dir)
    rows: list[dict] = []
    malformed: list[str] = []
    for path, doc in load_distributions_dir(head_dir):
        distro = doc.get("ros_distro") or path.stem
        for repo_name, spec in sorted((doc.get("repositories") or {}).items()):
            spec = spec or {}
            desired = registered_state(spec)
            ref = spec.get("ref") or {}
            if not (
                desired["url"]
                and ref.get("kind")
                and desired["ref"]["value"]
                and desired["packages"]
            ):
                # Same loud policy as sweep_matrix: a schema-valid file can
                # never hit this, and validate.yaml goes red on such a PR
                # anyway — a green build check next to it would be a lie.
                malformed.append(f"{path}::{repo_name}: missing url/ref/packages")
                continue

            if base.get((distro, repo_name)) == desired:
                continue
            rows.append(
                {
                    "ros_distro": distro,
                    "repo_name": repo_name,
                    "repository": desired["url"],
                    "ref_kind": ref["kind"],
                    "ref_value": desired["ref"]["value"],
                    "packages": " ".join(desired["packages"]),
                }
            )

    if malformed:
        raise RegistryError(
            "malformed repository entries (registered but unbuildable): " + "; ".join(malformed)
        )

    rows.sort(key=lambda r: (r["ros_distro"], r["repo_name"]))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir",
        required=True,
        help="BASE distributions dir (the PR's merge base)",
    )
    parser.add_argument(
        "--head-dir",
        required=True,
        help="HEAD distributions dir (the PR's result)",
    )
    parser.add_argument("--max-rows", type=int, default=MAX_ROWS_DEFAULT)
    parser.add_argument("--output", default="-", help="Output path; '-' for stdout")
    args = parser.parse_args()

    try:
        rows = build_matrix(Path(args.base_dir), Path(args.head_dir))
    except RegistryError as exc:
        sys.exit(f"::error::{exc}")
    except (AttributeError, TypeError) as exc:
        # Unlike every other caller of the tuple helpers, this script feeds
        # PRE-validation PR YAML into them: non-mapping garbage where the
        # schema promises mappings (`ref: main`, `packages: [a, b]`) throws
        # type errors deep inside. Keep that failure annotated instead of a
        # raw traceback — validate.yaml's schema check names the exact
        # offending field on the same PR.
        sys.exit(
            f"::error::a registry entry has a schema-invalid shape ({exc}); "
            f"the validate workflow's schema check pinpoints the field"
        )

    if len(rows) > args.max_rows:
        sys.exit(
            f"::error::build-check matrix has {len(rows)} rows, over the --max-rows guard of "
            f"{args.max_rows} (GitHub fails the whole matrix at 256); "
            f"split the registration into smaller pull requests"
        )

    payload = json.dumps({"include": rows}, separators=(",", ":"))
    if args.output == "-":
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
