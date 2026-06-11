#!/usr/bin/env python3
"""Cache each registered package's upstream package.xml on the data branch.

The browse card shows a one-line description per package. The source of truth
for that text is the package's own ``package.xml`` ``<description>`` — but
``site/build.py`` never clones the package repos, and the external sweep that
does check them out discards the working tree. So nothing persisted the
package.xml anywhere. This script is that missing cache step:

    discover  → matrix JSON (rows of (distro, package, repository, ref))
       │
    validate  → external sweep-package.yaml builds/tests each row
       │
    record    → append_history.py writes the pass/fail record, then
                THIS SCRIPT fetches each row's package.xml at the tested ref
                and writes it to metadata/<distro>/<package>.xml on the data
                branch, next to history/.

``build.py --metadata-dir`` reads those cached files at site-build time and
renders the ``<description>`` (unless a ``distributions/*.yaml`` entry sets its
own ``description:`` override, which wins).

A package repo may contain several ROS packages; the one whose ``<name>``
matches the registered package_name is the one cached.

Inputs (one of):
    --matrix-file PATH       sweep matrix {"include": [rows]} (the swept set)
    --distributions-dir DIR  fall back to every registered package

    --out DIR                where to write metadata/ files (default: _metadata)
    --push                   commit + push the cache to the data branch
                             (otherwise just writes --out locally, for preview)

Usage:
    scripts/cache_package_xml.py --matrix-file matrix.json --push
    scripts/cache_package_xml.py --distributions-dir distributions --out _metadata
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MAX_PUSH_RETRIES = 5


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def rows_from_matrix(matrix_file: Path) -> list[dict]:
    """Normalize sweep-matrix rows to {distro, package, repository, kind, value}."""
    import json

    matrix = json.loads(matrix_file.read_text())
    rows = []
    for row in matrix.get("include", []):
        rows.append(
            {
                "distro": row["ros_distro"],
                "package": row["package_name"],
                "repository": row["package_repository"],
                "kind": row.get("ref_kind", "branch"),
                "value": row["ref_value"],
            }
        )
    return rows


def rows_from_distributions(distributions_dir: Path) -> list[dict]:
    """Every registered package, regardless of when it was last swept."""
    rows = []
    for path in sorted(distributions_dir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        distro = doc.get("ros_distro") or path.stem
        for name, spec in (doc.get("packages") or {}).items():
            spec = spec or {}
            ref = spec.get("ref") or {}
            repository = spec.get("repository")
            value = ref.get("value")
            if not (repository and value):
                print(f"::warning::{path}::{name}: missing repository/ref; skipping", file=sys.stderr)
                continue
            rows.append(
                {
                    "distro": distro,
                    "package": name,
                    "repository": repository,
                    "kind": ref.get("kind", "branch"),
                    "value": value,
                }
            )
    return rows


def checkout(repository: str, kind: str, value: str, dest: Path) -> bool:
    """Shallow-checkout `repository` at `value` into `dest`. True on success."""
    if kind in ("branch", "tag"):
        result = run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--branch", value, repository, str(dest)],
            check=False,
        )
        return result.returncode == 0
    # sha: blobless clone, then check out the exact commit.
    if run(["git", "clone", "--filter=blob:none", repository, str(dest)], check=False).returncode != 0:
        return False
    return run(["git", "checkout", value], cwd=dest, check=False).returncode == 0


def find_package_xml(tree: Path, package_name: str) -> Path | None:
    """Return the package.xml whose <name> equals package_name, if any."""
    for path in sorted(tree.glob("**/package.xml")):
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8"))
        except (ET.ParseError, OSError):
            continue
        name = root.find("name")
        if name is not None and (name.text or "").strip() == package_name:
            return path
    return None


def cache_one(row: dict, out_dir: Path) -> bool:
    """Fetch one package's package.xml into out_dir/<distro>/<package>.xml."""
    tmp = Path(tempfile.mkdtemp(prefix="pkgxml-"))
    try:
        if not checkout(row["repository"], row["kind"], row["value"], tmp):
            print(
                f"::error::{row['distro']}/{row['package']}: could not check out "
                f"{row['kind']} {row['value']} from {row['repository']}",
                file=sys.stderr,
            )
            return False
        package_xml = find_package_xml(tmp, row["package"])
        if package_xml is None:
            print(
                f"::error::{row['distro']}/{row['package']}: no package.xml with "
                f"<name>{row['package']}</name> found in {row['repository']}",
                file=sys.stderr,
            )
            return False
        target_dir = out_dir / row["distro"]
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(package_xml, target_dir / f"{row['package']}.xml")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def push_metadata(staged: Path, rows: list[dict]) -> None:
    """Commit staged metadata/ files onto the data branch (worktree + retry)."""
    repo_root = Path.cwd()
    tmpdir = Path(tempfile.mkdtemp(prefix="data-worktree-"))
    try:
        run(["git", "fetch", "origin", "data"], repo_root)
        run(["git", "worktree", "add", str(tmpdir), "origin/data"], repo_root)
        run(["git", "config", "user.name", BOT_NAME], tmpdir)
        run(["git", "config", "user.email", BOT_EMAIL], tmpdir)

        for attempt in range(1, MAX_PUSH_RETRIES + 1):
            run(["git", "fetch", "origin", "data"], tmpdir)
            run(["git", "reset", "--hard", "origin/data"], tmpdir)

            # Merge the staged files over the existing tree — never replace it.
            # A sweep stages only the packages it swept; wiping metadata/ first
            # would delete every non-swept package's cached package.xml.
            dest = tmpdir / "metadata"
            shutil.copytree(staged, dest, dirs_exist_ok=True)

            run(["git", "add", "metadata/"], tmpdir)
            if not run(["git", "status", "--porcelain"], tmpdir).stdout.strip():
                print("metadata cache already up to date", file=sys.stderr)
                return

            run(["git", "commit", "-m", commit_message(rows)], tmpdir)
            push = run(["git", "push", "origin", "HEAD:data"], tmpdir, check=False)
            if push.returncode == 0:
                print(f"pushed metadata cache on attempt {attempt}", file=sys.stderr)
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


def commit_message(rows: list[dict]) -> str:
    if len(rows) == 1:
        r = rows[0]
        return f"chore(data): cache package.xml for {r['package']} @ {r['distro']}"
    distros = sorted({r["distro"] for r in rows})
    return f"chore(data): cache {len(rows)} package.xml file(s) across {','.join(distros)}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--matrix-file", help="sweep matrix JSON (the swept set)")
    source.add_argument("--distributions-dir", help="cache every registered package")
    parser.add_argument("--out", default="_metadata", help="local output dir for metadata/ files")
    parser.add_argument("--push", action="store_true", help="commit + push the cache to the data branch")
    args = parser.parse_args()

    if args.matrix_file:
        rows = rows_from_matrix(Path(args.matrix_file))
    else:
        rows = rows_from_distributions(Path(args.distributions_dir))

    if not rows:
        print("no packages to cache", file=sys.stderr)
        return

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cached = [row for row in rows if cache_one(row, out_dir)]
    print(f"cached {len(cached)}/{len(rows)} package.xml file(s) -> {out_dir}", file=sys.stderr)

    if args.push and cached:
        push_metadata(out_dir, cached)


if __name__ == "__main__":
    main()
