#!/usr/bin/env python3
r"""Write one sweep's outputs to the orphan data branch in a single commit.

Three trees land together, atomically per push:

  history/<distro>/<package>.ndjson   append-only validation records (one
                                      line per envelope; never rewritten)
  state/<distro>/<repo_name>.json     mutable per-repository cursor: the
                                      (url, ref, package set) that was last
                                      CONCLUSIVELY recorded. sweep_matrix.py
                                      diffs the registry against these to
                                      decide what to sweep (level-trigger).
                                      Written in the SAME commit as the
                                      history lines, so "state advanced"
                                      implies "records landed".
  metadata/<distro>/<package>.xml     mutable cache of each package's pristine
                                      upstream package.xml (site descriptions);
                                      merged over the existing tree, never
                                      replacing it.

Envelopes come from build_envelopes.py:

    {
      "ros_distro": "jazzy",
      "package_name": "autoware_livox_tag_filter",
      "record": { ... conforming to schema/history-record.schema.json ... }
    }

States likewise: {"ros_distro", "repo_name", "state": {...}}.

The script creates a temporary git worktree on the data branch, writes,
commits, and pushes. On push conflict (someone else wrote to data between our
fetch and push) it retries with a fresh fetch; every write step is
re-applied per attempt on top of the fresh tip, so the normal retry path is
exactly-once. (Known residual edge: a "phantom" push, where the server applies
the update but the client sees an error, would re-append the same lines on the
next attempt. Accepted: rare, and the duplicate lines are self-describing and
harmless to the site's latest-by-timestamp summarize.)

The sweep workflows' record jobs must declare
`concurrency: { group: data-branch-write, cancel-in-progress: false }` so
multiple record jobs queue rather than race. (Only the record JOB serializes;
validate jobs of overlapping sweeps run in parallel. A record job that gets
displaced from the queue is harmless: its state files never advanced, so the
level-triggered discover re-detects the work.)

Usage:
    scripts/append_history.py --records envelopes.json \
        [--states states.json] [--metadata-dir staged-metadata]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MAX_PUSH_RETRIES = 5


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def _existing_state_is_newer(target_file: Path, state: dict) -> bool:
    """Return True when the on-branch cursor carries a strictly newer `at` timestamp."""
    try:
        existing = json.loads(target_file.read_text(encoding="utf-8"))
        return str(existing.get("at", "")) > str(state.get("at", ""))
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return False


def write_outputs(
    tmpdir: Path, envelopes: list[dict], states: list[dict], metadata_dir: Path | None
) -> None:
    """Apply one attempt's writes onto a fresh data-branch worktree."""
    for env in envelopes:
        target_dir = tmpdir / "history" / env["ros_distro"]
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / f"{env['package_name']}.ndjson"
        with target_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(env["record"], separators=(",", ":")) + "\n")

    for entry in states:
        target_dir = tmpdir / "state" / entry["ros_distro"]
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / f"{entry['repo_name']}.json"
        if _existing_state_is_newer(target_file, entry["state"]):
            # Out-of-order record jobs: a slower run that swept an OLDER
            # registration must not roll the cursor back over a fresher one
            # (its history lines still append: history is append-only and
            # per-line self-describing; only the cursor keeps the newest).
            print(
                f"state {entry['ros_distro']}/{entry['repo_name']} already newer; not regressing it",
                file=sys.stderr,
            )
            continue
        target_file.write_text(json.dumps(entry["state"], indent=2) + "\n", encoding="utf-8")

    if metadata_dir is not None and metadata_dir.is_dir():
        # Merge over the existing cache: a sweep stages only what it swept;
        # replacing the tree would delete every non-swept package's file.
        shutil.copytree(metadata_dir, tmpdir / "metadata", dirs_exist_ok=True)


def append_history(envelopes: list[dict], states: list[dict], metadata_dir: Path | None) -> None:
    if not envelopes:
        print("no records to append", file=sys.stderr)
        return

    repo_root = Path.cwd()
    tmpdir = Path(tempfile.mkdtemp(prefix="data-worktree-"))
    try:
        run(["git", "fetch", "origin", "data"], repo_root)
        run(["git", "worktree", "add", str(tmpdir), "origin/data"], repo_root)

        for attempt in range(1, MAX_PUSH_RETRIES + 1):
            # Refresh to current tip of data before writing.
            run(["git", "fetch", "origin", "data"], tmpdir)
            run(["git", "reset", "--hard", "origin/data"], tmpdir)

            write_outputs(tmpdir, envelopes, states, metadata_dir)

            run(["git", "add", "-A"], tmpdir)
            status = run(["git", "status", "--porcelain"], tmpdir).stdout.strip()
            if not status:
                print("no changes to commit", file=sys.stderr)
                return

            # Per-command identity: `git config user.*` in a linked worktree
            # writes the SHARED repo config; a local run would leave the
            # operator's clone authoring everything as the bot.
            run(
                [
                    "git",
                    "-c",
                    f"user.name={BOT_NAME}",
                    "-c",
                    f"user.email={BOT_EMAIL}",
                    "commit",
                    "-m",
                    build_commit_message(envelopes),
                ],
                tmpdir,
            )

            push = subprocess.run(
                ["git", "push", "origin", "HEAD:data"],
                cwd=tmpdir,
                capture_output=True,
                text=True,
            )
            if push.returncode == 0:
                print(
                    f"pushed {len(envelopes)} history record(s), {len(states)} state "
                    f"advance(s) on attempt {attempt}",
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
    def repo_of(env: dict) -> str:
        return env["record"].get("repo_name") or env["package_name"]

    if len(envelopes) == 1:
        e = envelopes[0]
        r = e["record"]
        return (
            f"chore(data): {r['sweep_kind']} sweep {r['status']} "
            f"for {e['package_name']} ({repo_of(e)}) @ {e['ros_distro']} "
            f"/ autoware {r['autoware_version']}"
        )
    kinds = sorted({e["record"]["sweep_kind"] for e in envelopes})
    distros = sorted({e["ros_distro"] for e in envelopes})
    repos = sorted({repo_of(e) for e in envelopes})
    return (
        f"chore(data): append {len(envelopes)} sweep result(s) "
        f"[{','.join(kinds)}] for {','.join(repos)} across {','.join(distros)}"
    )


def load_json(source: str, what: str) -> list[dict]:
    if source == "-":
        payload = json.load(sys.stdin)
    else:
        with open(source, encoding="utf-8") as f:
            payload = json.load(f)
    if not isinstance(payload, list):
        sys.exit(f"{what} JSON must be an array")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--records",
        required=True,
        help="Path to JSON file with envelope array, or '-' for stdin.",
    )
    parser.add_argument(
        "--states",
        default="",
        help="Path to JSON file with state-advance array (from build_envelopes.py).",
    )
    parser.add_argument(
        "--metadata-dir",
        default="",
        help="Dir of staged metadata/<distro>/<pkg>.xml files to merge onto the branch.",
    )
    args = parser.parse_args()
    envelopes = load_json(args.records, "records")
    states = load_json(args.states, "states") if args.states else []
    metadata_dir = Path(args.metadata_dir) if args.metadata_dir else None
    append_history(envelopes, states, metadata_dir)


if __name__ == "__main__":
    main()
