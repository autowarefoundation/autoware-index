#!/usr/bin/env python3
"""Emit the sweep matrix: one row per (distro, repository), level-triggered.

Replaces the edge-triggered sweep_eager_matrix.py (git diff of before/after
shas) and sweep_nightly_matrix.py (every branch ref). Both modes now diff the
DESIRED state (distributions/*.yaml on main) against the LAST CONCLUSIVELY
RECORDED state (state/<distro>/<repo_name>.json on the data branch, written by
append_history.py in the same commit as the history records):

  --mode eager     rows for every repository entry whose (url, ref, registered
                   package set, index dependency closure) differs from its state file, or that has no
                   state file yet. A run that was cancelled, lost to
                   concurrency, or interrupted before recording leaves its
                   state stale, so the NEXT eager or nightly run re-detects
                   the same delta: lost sweeps self-heal instead of silently
                   never producing a record. URL-only changes (the classic
                   monorepo-consolidation edit) trigger too; the old ref-only
                   git diff missed them.

  --mode nightly   every `kind: branch` repository (tips move under a fixed
                   ref value), plus consumers of a branch dependency, UNION
                   the eager state-diff as catch-up.

Pinned tag/sha repositories with an up-to-date state file are swept by
neither mode unless they consume a branch dependency.

Row shape (consumed by sweep-repository.yaml in the actions repo and by
scripts/build_envelopes.py; `packages` is space-separated for workflow_call
string inputs):

    {
      "ros_distro": "jazzy",
      "repo_name": "awesome_tools",
      "repository": "https://github.com/example-org/awesome_tools",
      "ref_kind": "tag",
      "ref_value": "1.2.0",
      "packages": "autoware_a_filter zz_planner_b"
    }

GitHub caps a job matrix at 256 rows; a sweep that exceeds the cap fails at
strategy expansion AFTER discover, recording nothing, silently. So discover
itself enforces --max-rows (default 250) and fails LOUDLY here instead.
Chunking across multiple workflow runs is the documented follow-up when the
registry approaches that many repositories.

Usage:
    scripts/sweep_matrix.py --mode eager   --state-dir _data/state
    scripts/sweep_matrix.py --mode nightly --state-dir _data/state [--output -]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from registry_load import RegistryError
from registry_load import load_distributions_dir

MAX_ROWS_DEFAULT = 250


def dependency_context(doc: dict, repo_name: str) -> dict:
    """Resolve a repository's registered package dependencies within one distro.

    The context is part of the sweep state: changing a dependency edge or a
    transitive repository ref must revalidate packages that consume it.
    """
    repositories = doc.get("repositories") or {}
    owners = {
        package: (name, spec)
        for name, spec in repositories.items()
        for package in (spec.get("packages") or {})
    }
    # Pre-validation PR fixtures may contain duplicate package keys. The
    # semantic gate rejects them, but a row's own packages must still be
    # attributed to that row while the build-check matrix is computed.
    for package in repositories[repo_name].get("packages") or {}:
        owners[package] = (repo_name, repositories[repo_name])
    graph: dict[str, list[str]] = {}
    external: dict[str, dict] = {}
    required_packages: set[str] = set()
    visited: set[str] = set()
    active: list[str] = []

    def visit(package: str) -> None:
        if package not in owners:
            raise RegistryError(f"index dependency {package!r} is not registered")
        if package in active:
            cycle = active[active.index(package) :] + [package]
            raise RegistryError(f"index dependency cycle: {' -> '.join(cycle)}")
        if package in visited:
            return
        active.append(package)
        owner, spec = owners[package]
        if owner != repo_name:
            external[owner] = {
                "repo_name": owner,
                "repository": spec.get("url", ""),
                "ref_kind": (spec.get("ref") or {}).get("kind", ""),
                "ref_value": str((spec.get("ref") or {}).get("value", "")),
            }
        dependencies = ((spec.get("packages") or {})[package] or {}).get("index_dependencies") or []
        if dependencies:
            graph[package] = sorted(dependencies)
            required_packages.update(dependencies)
        for target in dependencies:
            visit(target)
        active.pop()
        visited.add(package)

    for package in sorted((repositories[repo_name].get("packages") or {})):
        visit(package)
    return {
        "index_dependencies": {name: graph[name] for name in sorted(graph)},
        "dependency_repositories": [external[name] for name in sorted(external)],
        "dependency_packages": sorted(required_packages),
    }


def registered_state(spec: dict, context: dict | None = None) -> dict:
    """Build the source/dependency tuple a state file is diffed against."""
    ref = spec.get("ref") or {}
    state = {
        "url": spec.get("url", ""),
        "ref": {"kind": ref.get("kind", ""), "value": str(ref.get("value", ""))},
        "packages": sorted((spec.get("packages") or {}).keys()),
    }
    for key in ("index_dependencies", "dependency_repositories", "dependency_packages"):
        if context and context.get(key):
            state[key] = context[key]
    return state


def recorded_state(state_dir: Path, distro: str, repo_name: str) -> dict | None:
    """Parse state/<distro>/<repo_name>.json; None when absent or unreadable."""
    path = state_dir / distro / f"{repo_name}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    ref = doc.get("ref") or {}
    state = {
        "url": doc.get("url", ""),
        "ref": {"kind": ref.get("kind", ""), "value": str(ref.get("value", ""))},
        "packages": sorted(doc.get("packages") or []),
    }
    for key in ("index_dependencies", "dependency_repositories", "dependency_packages"):
        if doc.get(key):
            state[key] = doc[key]
    return state


def matrix_row(distro: str, repo_name: str, desired: dict) -> dict:
    """Build one workflow_call row, retaining dependency state for recording."""
    row = {
        "ros_distro": distro,
        "repo_name": repo_name,
        "repository": desired["url"],
        "ref_kind": desired["ref"]["kind"],
        "ref_value": desired["ref"]["value"],
        "packages": " ".join(desired["packages"]),
    }
    if desired.get("index_dependencies"):
        row["index_dependencies"] = desired["index_dependencies"]
    if desired.get("dependency_repositories"):
        row["dependency_repositories"] = desired["dependency_repositories"]
    if desired.get("dependency_packages"):
        row["dependency_packages"] = " ".join(desired["dependency_packages"])
    return row


def build_matrix(distributions_dir: Path, state_dir: Path, mode: str) -> list[dict]:
    rows: list[dict] = []
    malformed: list[str] = []
    for path, doc in load_distributions_dir(distributions_dir):
        distro = doc.get("ros_distro") or path.stem
        for repo_name, spec in sorted((doc.get("repositories") or {}).items()):
            spec = spec or {}
            desired = registered_state(spec, dependency_context(doc, repo_name))
            ref = spec.get("ref") or {}
            if not (
                desired["url"]
                and ref.get("kind")
                and desired["ref"]["value"]
                and desired["packages"]
            ):
                # A schema-valid file can never hit this (url/ref/packages are
                # all required); reaching it means something bypassed the PR
                # gate. Soft-skipping would leave the entry registered but
                # silently never swept, with every job green; collect it and
                # fail discover loudly instead (raised after the loop so all
                # offenders are reported at once).
                malformed.append(f"{path}::{repo_name}: missing url/ref/packages")
                continue

            is_branch = ref.get("kind") == "branch" or any(
                dep["ref_kind"] == "branch" for dep in desired.get("dependency_repositories", [])
            )
            differs = recorded_state(state_dir, distro, repo_name) != desired
            if (mode == "eager" and differs) or (mode == "nightly" and (is_branch or differs)):
                rows.append(matrix_row(distro, repo_name, desired))

    if malformed:
        raise RegistryError(
            "malformed repository entries (registered but unsweepable): " + "; ".join(malformed)
        )

    rows.sort(key=lambda r: (r["ros_distro"], r["repo_name"]))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=["eager", "nightly"])
    parser.add_argument("--distributions-dir", default="distributions")
    parser.add_argument(
        "--state-dir",
        required=True,
        help="Path to the data branch's state/ dir (missing/empty = sweep everything)",
    )
    parser.add_argument("--max-rows", type=int, default=MAX_ROWS_DEFAULT)
    parser.add_argument("--output", default="-", help="Output path; '-' for stdout")
    args = parser.parse_args()

    try:
        rows = build_matrix(Path(args.distributions_dir), Path(args.state_dir), args.mode)
    except RegistryError as exc:
        sys.exit(f"::error::{exc}")

    if len(rows) > args.max_rows:
        sys.exit(
            f"::error::sweep matrix has {len(rows)} rows, over the --max-rows guard of "
            f"{args.max_rows} (GitHub fails the whole matrix at 256, recording nothing); "
            f"shard the sweep before registering more repositories"
        )

    payload = json.dumps({"include": rows}, separators=(",", ":"))
    if args.output == "-":
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
