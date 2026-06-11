#!/usr/bin/env python3
"""Emit the sweep matrix: one row per (distro, repository), level-triggered.

Replaces the edge-triggered sweep_eager_matrix.py (git diff of before/after
shas) and sweep_nightly_matrix.py (every branch ref). Both modes now diff the
DESIRED state (distributions/*.yaml on main) against the LAST CONCLUSIVELY
RECORDED state (state/<distro>/<repo_name>.json on the data branch, written by
append_history.py in the same commit as the history records):

  --mode eager     rows for every repository entry whose (url, ref, registered
                   package set) differs from its state file — or that has no
                   state file yet. A run that was cancelled, lost to
                   concurrency, or interrupted before recording leaves its
                   state stale, so the NEXT eager or nightly run re-detects
                   the same delta: lost sweeps self-heal instead of silently
                   never producing a record. URL-only changes (the classic
                   monorepo-consolidation edit) trigger too — the old ref-only
                   git diff missed them.

  --mode nightly   every `kind: branch` repository (tips move under a fixed
                   ref value), UNION the eager state-diff as catch-up.

Pinned tag/sha repositories with an up-to-date state file are swept by
neither mode: re-testing the same immutable ref adds nothing (a new Autoware
release is picked up when the ref next changes, or nightly for branch refs).

Row shape (consumed by sweep-repository.yaml in the actions repo and by
scripts/build_envelopes.py — `packages` is space-separated for workflow_call
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
import sys
from pathlib import Path

from registry_load import RegistryError, load_distributions_dir

MAX_ROWS_DEFAULT = 250


def registered_state(spec: dict) -> dict:
    """The (url, ref, package set) tuple a state file is diffed against."""
    ref = spec.get("ref") or {}
    return {
        "url": spec.get("url", ""),
        "ref": {"kind": ref.get("kind", ""), "value": str(ref.get("value", ""))},
        "packages": sorted((spec.get("packages") or {}).keys()),
    }


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
    return {
        "url": doc.get("url", ""),
        "ref": {"kind": ref.get("kind", ""), "value": str(ref.get("value", ""))},
        "packages": sorted(doc.get("packages") or []),
    }


def build_matrix(distributions_dir: Path, state_dir: Path, mode: str) -> list[dict]:
    rows: list[dict] = []
    for path, doc in load_distributions_dir(distributions_dir):
        distro = doc.get("ros_distro") or path.stem
        for repo_name, spec in sorted((doc.get("repositories") or {}).items()):
            spec = spec or {}
            desired = registered_state(spec)
            ref = spec.get("ref") or {}
            if not (desired["url"] and ref.get("kind") and desired["ref"]["value"] and desired["packages"]):
                print(
                    f"::error::{path}::{repo_name}: missing url/ref/packages; row skipped",
                    file=sys.stderr,
                )
                continue

            is_branch = ref.get("kind") == "branch"
            differs = recorded_state(state_dir, distro, repo_name) != desired
            if (mode == "eager" and differs) or (mode == "nightly" and (is_branch or differs)):
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
