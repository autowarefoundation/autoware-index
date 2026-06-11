#!/usr/bin/env python3
"""Backfill registered packages' upstream package.xml onto the data branch.

The browse card shows a one-line description per package, sourced from the
package's own ``package.xml`` ``<description>`` cached at
``metadata/<distro>/<package>.xml`` on the data branch (a registry-side
``description:`` override wins when set).

In the normal pipeline the SWEEP populates that cache: sweep-repository.yaml
ships every present package's pristine package.xml inside its result artifact,
and the record job stages those via build_envelopes.py + append_history.py —
no extra clone. This script is the MANUAL/BACKFILL path for everything the
sweep hasn't covered (e.g. seeding the cache after the schema-2 cutover, or
repairing a gap):

    scripts/cache_package_xml.py --distributions-dir distributions --push

It clones each registered REPOSITORY once at its registered ref (however many
packages it hosts), picks each package's package.xml by matching the
``<name>`` element, and merge-pushes the files to the data branch.

    --out DIR   where to write metadata/ files (default: _metadata)
    --push      commit + push the cache to the data branch
                (otherwise just writes --out locally, for preview)
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

from registry_load import RegistryError, load_distributions_dir

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MAX_PUSH_RETRIES = 5


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def repo_groups_from_distributions(distributions_dir: Path) -> list[dict]:
    """One group per registered repository: clone once, extract N package.xmls."""
    try:
        loaded = load_distributions_dir(distributions_dir)
    except RegistryError as exc:
        sys.exit(f"::error::{exc}")

    groups: list[dict] = []
    for path, doc in loaded:
        distro = doc.get("ros_distro") or path.stem
        for repo_name, spec in sorted((doc.get("repositories") or {}).items()):
            spec = spec or {}
            ref = spec.get("ref") or {}
            url = spec.get("url")
            value = ref.get("value")
            packages = sorted((spec.get("packages") or {}).keys())
            if not (url and value and packages):
                print(
                    f"::warning::{path}::{repo_name}: missing url/ref/packages; skipping",
                    file=sys.stderr,
                )
                continue
            groups.append(
                {
                    "distro": distro,
                    "repo_name": repo_name,
                    "repository": url,
                    "kind": ref.get("kind", "branch"),
                    "value": str(value),
                    "packages": packages,
                }
            )
    return groups


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


def cache_group(group: dict, out_dir: Path) -> list[dict]:
    """Clone one repository, cache every registered package's package.xml.

    Returns one {distro, package} row per successfully cached file.
    """
    cached: list[dict] = []
    tmp = Path(tempfile.mkdtemp(prefix="pkgxml-"))
    try:
        if not checkout(group["repository"], group["kind"], group["value"], tmp):
            print(
                f"::error::{group['distro']}/{group['repo_name']}: could not check out "
                f"{group['kind']} {group['value']} from {group['repository']}",
                file=sys.stderr,
            )
            return cached
        for package in group["packages"]:
            package_xml = find_package_xml(tmp, package)
            if package_xml is None:
                print(
                    f"::error::{group['distro']}/{package}: no package.xml with "
                    f"<name>{package}</name> found in {group['repository']}",
                    file=sys.stderr,
                )
                continue
            target_dir = out_dir / group["distro"]
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package_xml, target_dir / f"{package}.xml")
            cached.append({"distro": group["distro"], "package": package})
        return cached
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def push_metadata(staged: Path, rows: list[dict]) -> None:
    """Commit staged metadata/ files onto the data branch (worktree + retry)."""
    repo_root = Path.cwd()
    tmpdir = Path(tempfile.mkdtemp(prefix="data-worktree-"))
    try:
        run(["git", "fetch", "origin", "data"], repo_root)
        run(["git", "worktree", "add", str(tmpdir), "origin/data"], repo_root)

        for attempt in range(1, MAX_PUSH_RETRIES + 1):
            run(["git", "fetch", "origin", "data"], tmpdir)
            run(["git", "reset", "--hard", "origin/data"], tmpdir)

            # Merge the staged files over the existing tree — never replace it.
            # A backfill stages only what it could cache; wiping metadata/
            # first would delete every other package's cached package.xml.
            dest = tmpdir / "metadata"
            shutil.copytree(staged, dest, dirs_exist_ok=True)

            run(["git", "add", "metadata/"], tmpdir)
            if not run(["git", "status", "--porcelain"], tmpdir).stdout.strip():
                print("metadata cache already up to date", file=sys.stderr)
                return

            # Per-command identity: `git config user.*` in a linked worktree
            # writes the SHARED repo config — this script is the documented
            # MANUAL backfill path, and a local run would leave the operator's
            # clone authoring every later commit as the bot.
            run(
                [
                    "git",
                    "-c", f"user.name={BOT_NAME}",
                    "-c", f"user.email={BOT_EMAIL}",
                    "commit", "-m", commit_message(rows),
                ],
                tmpdir,
            )
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
    parser.add_argument("--distributions-dir", required=True, help="cache every registered package")
    parser.add_argument("--out", default="_metadata", help="local output dir for metadata/ files")
    parser.add_argument("--push", action="store_true", help="commit + push the cache to the data branch")
    args = parser.parse_args()

    groups = repo_groups_from_distributions(Path(args.distributions_dir))
    if not groups:
        print("no repositories to cache", file=sys.stderr)
        return

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cached = [row for group in groups for row in cache_group(group, out_dir)]
    total = sum(len(g["packages"]) for g in groups)
    print(
        f"cached {len(cached)}/{total} package.xml file(s) from {len(groups)} "
        f"repository clone(s) -> {out_dir}",
        file=sys.stderr,
    )

    if not cached:
        sys.exit("::error::no package.xml could be cached for any registered package")

    if args.push:
        push_metadata(out_dir, cached)


if __name__ == "__main__":
    main()
