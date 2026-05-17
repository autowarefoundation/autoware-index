#!/usr/bin/env python3
"""Append validation history records to the orphan data branch.

Reads "envelope" records from a JSON file (or stdin) and routes each record
to history/<ros_distro>/<package_name>.ndjson on the data branch. Each
envelope has the shape:

    {
      "ros_distro": "jazzy",
      "package_name": "autoware_livox_tag_filter",
      "record": { ... conforming to schema/history-record.schema.json ... }
    }

The script creates a temporary git worktree on the data branch, appends the
records, commits, and pushes. On push conflict (someone else wrote to data
between our fetch and push) it retries with a fresh fetch.

Sweep workflows that call this script must declare
`concurrency: { group: data-branch-write, cancel-in-progress: false }` so
multiple sweeps queue rather than race.

Usage:
    scripts/append_history.py --records records.json
    scripts/append_history.py --records - < records.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MAX_PUSH_RETRIES = 5


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def append_history(envelopes: list[dict]) -> None:
    if not envelopes:
        print("no records to append", file=sys.stderr)
        return

    repo_root = Path.cwd()
    tmpdir = Path(tempfile.mkdtemp(prefix="data-worktree-"))
    try:
        run(["git", "fetch", "origin", "data"], repo_root)
        run(["git", "worktree", "add", str(tmpdir), "origin/data"], repo_root)
        run(["git", "config", "user.name", BOT_NAME], tmpdir)
        run(["git", "config", "user.email", BOT_EMAIL], tmpdir)

        for attempt in range(1, MAX_PUSH_RETRIES + 1):
            # Refresh to current tip of data before appending.
            run(["git", "fetch", "origin", "data"], tmpdir)
            run(["git", "reset", "--hard", "origin/data"], tmpdir)

            for env in envelopes:
                distro = env["ros_distro"]
                package = env["package_name"]
                record = env["record"]
                target_dir = tmpdir / "history" / distro
                target_dir.mkdir(parents=True, exist_ok=True)
                target_file = target_dir / f"{package}.ndjson"
                with target_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, separators=(",", ":")) + "\n")

            run(["git", "add", "history/"], tmpdir)
            status = run(["git", "status", "--porcelain"], tmpdir).stdout.strip()
            if not status:
                print("no changes to commit", file=sys.stderr)
                return

            run(["git", "commit", "-m", build_commit_message(envelopes)], tmpdir)

            push = subprocess.run(
                ["git", "push", "origin", "HEAD:data"],
                cwd=tmpdir,
                capture_output=True,
                text=True,
            )
            if push.returncode == 0:
                print(
                    f"pushed {len(envelopes)} history record(s) on attempt {attempt}",
                    file=sys.stderr,
                )
                return

            print(
                f"push attempt {attempt}/{MAX_PUSH_RETRIES} failed: {push.stderr.strip()}",
                file=sys.stderr,
            )
            if attempt < MAX_PUSH_RETRIES:
                time.sleep(2 ** (attempt - 1))

        sys.exit(f"push failed after {MAX_PUSH_RETRIES} attempts")
    finally:
        run(["git", "worktree", "remove", "--force", str(tmpdir)], repo_root, check=False)


def build_commit_message(envelopes: list[dict]) -> str:
    if len(envelopes) == 1:
        e = envelopes[0]
        r = e["record"]
        return (
            f"chore(data): {r['sweep_kind']} sweep {r['status']} "
            f"for {e['package_name']} @ {e['ros_distro']} / autoware {r['autoware_version']}"
        )
    kinds = sorted({e["record"]["sweep_kind"] for e in envelopes})
    distros = sorted({e["ros_distro"] for e in envelopes})
    return (
        f"chore(data): append {len(envelopes)} sweep result(s) "
        f"[{','.join(kinds)}] across {','.join(distros)}"
    )


def load_envelopes(source: str) -> list[dict]:
    if source == "-":
        envelopes = json.load(sys.stdin)
    else:
        with open(source, encoding="utf-8") as f:
            envelopes = json.load(f)
    if not isinstance(envelopes, list):
        sys.exit("records JSON must be an array of envelopes")
    return envelopes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--records",
        required=True,
        help="Path to JSON file with envelope array, or '-' for stdin.",
    )
    args = parser.parse_args()
    envelopes = load_envelopes(args.records)
    append_history(envelopes)


if __name__ == "__main__":
    main()
