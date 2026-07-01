#!/usr/bin/env python3
r"""Export the Autoware Index browse data and assemble the deployable site.

The browse site is a static front-end (index.html + styles.css + app.js) that
renders client-side from a generated data.json. This script is just the data
exporter + assembler: it joins the two branches the data lives on —

  - main:  distributions/<distro>.yaml          (what is registered)
  - data:  history/<distro>/<package>.ndjson    (how it has validated)
  - data:  metadata/<distro>/<package>.xml      (cached upstream package.xml)

— summarizes each package, resolves its card description (registry override, or
else the cached package.xml <description>), writes data.json, and copies the
static assets next to it so --out is a complete, deployable directory.

In CI the `data` branch is checked out into a sibling path and passed via
--history-dir; locally you can point --history-dir at site/sample-data/history
to preview a populated table before any real sweep has run. Because app.js
fetches data.json, preview over an HTTP server (fetch is blocked over file://):

    site/build.py --history-dir site/sample-data/history --out _site
    python -m http.server -d _site     # then open http://localhost:8000

Usage:
    site/build.py --distributions-dir distributions \
                  --history-dir _data/history \
                  --out _site
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

SITE_DIR = Path(__file__).resolve().parent
STATIC_ASSETS = ("index.html", "styles.css", "app.js", "compose.mjs")

sys.path.insert(0, str(SITE_DIR.parent / "scripts"))
from registry_load import RegistryError  # noqa: E402
from registry_load import flatten_packages  # noqa: E402
from registry_load import load_distributions_dir  # noqa: E402


def semver_key(version: str) -> tuple:
    """Sort key for an X.Y.Z SemVer string; non-numeric parts sort last."""
    parts = []
    for piece in str(version).split("."):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def load_distributions(distributions_dir: Path) -> list[dict]:
    """Flatten distributions/*.yaml into one registration record per package.

    Goes through the shared version-gated loader: an unsupported
    schema_version is a HARD build failure, never a silently empty site.
    The package -> repository mapping is computed here at load time; cards
    stay keyed by (distro, package) exactly as before, with repo_name as a
    display/group field.
    """
    registrations: list[dict] = []
    for path, doc in load_distributions_dir(distributions_dir):
        distro = doc.get("ros_distro") or path.stem
        for rec in flatten_packages(doc, distro=distro):
            registrations.append(
                {
                    "distro": rec["distro"],
                    "name": rec["package"],
                    "repo_name": rec["repo_name"],
                    "repository": rec["repository"],
                    "description": rec["description"],
                    "governance": rec["governance"],
                    "tags": rec["tags"],
                    "maintainers": rec["maintainers"],
                    "ref": rec["ref"],
                }
            )
    return registrations


def parse_description(package_xml: str) -> str:
    """Extract <description> from a package.xml string, whitespace-normalized."""
    try:
        root = ET.fromstring(package_xml)
    except ET.ParseError:
        return ""
    node = root.find("description")
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def load_metadata(metadata_dir: Path) -> dict[tuple[str, str], str]:
    """Read cached metadata/<distro>/<package>.xml into {(distro, package): description}.

    The sweep caches each swept package's upstream package.xml here so the site
    can render an upstream-sourced description without re-fetching at build time.
    """
    descriptions: dict[tuple[str, str], str] = {}
    if not metadata_dir or not metadata_dir.is_dir():
        return descriptions
    for package_xml in sorted(metadata_dir.glob("*/*.xml")):
        distro = package_xml.parent.name
        package = package_xml.stem
        descriptions[(distro, package)] = parse_description(package_xml.read_text(encoding="utf-8"))
    return descriptions


def load_history(history_dir: Path) -> dict[tuple[str, str], list[dict]]:
    """Read history/<distro>/<package>.ndjson into {(distro, package): [records]}."""
    history: dict[tuple[str, str], list[dict]] = {}
    if not history_dir or not history_dir.is_dir():
        return history
    for ndjson in sorted(history_dir.glob("*/*.ndjson")):
        distro = ndjson.parent.name
        package = ndjson.stem
        records = []
        for line in ndjson.read_text().splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        history[(distro, package)] = records
    return history


def summarize(records: list[dict]) -> dict:
    """Derive current status, last-green version, and a per-version latest cell."""
    if not records:
        return {
            "current_status": "unknown",
            "last_green": None,
            "last_tested_at": None,
            "versions": [],
        }

    by_time = sorted(records, key=lambda r: r.get("at", ""))
    latest_overall = by_time[-1]

    # Latest record per autoware_version (most recent `at` wins).
    latest_per_version: dict[str, dict] = {}
    for rec in by_time:
        latest_per_version[rec.get("autoware_version", "?")] = rec

    greens = [r for r in by_time if r.get("status") == "pass"]
    last_green = greens[-1].get("autoware_version") if greens else None

    versions = [
        {
            "autoware_version": ver,
            "status": rec.get("status", "unknown"),
            "ref_at_test": rec.get("ref_at_test", {}),
            "resolved_sha": rec.get("resolved_sha", ""),
            "at": rec.get("at", ""),
            "actions_run_url": rec.get("actions_run_url", ""),
        }
        for ver, rec in sorted(
            latest_per_version.items(), key=lambda kv: semver_key(kv[0]), reverse=True
        )
    ]

    return {
        "current_status": latest_overall.get("status", "unknown"),
        "last_green": last_green,
        "last_tested_at": latest_overall.get("at"),
        "versions": versions,
    }


def build_packages(
    registrations: list[dict],
    history: dict[tuple[str, str], list[dict]],
    metadata: dict[tuple[str, str], str],
) -> list[dict]:
    packages = []
    for reg in registrations:
        key = (reg["distro"], reg["name"])
        records = history.get(key, [])
        # Registry-side override wins; otherwise fall back to the cached
        # upstream package.xml <description>.
        description = reg["description"] or metadata.get(key, "")
        packages.append({**reg, **summarize(records), "description": description})
    packages.sort(key=lambda p: (p["name"], p["distro"]))
    return packages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distributions-dir", default="distributions")
    parser.add_argument("--history-dir", default="", help="Path to the data branch's history/ dir")
    parser.add_argument(
        "--metadata-dir", default="", help="Path to the data branch's metadata/ dir"
    )
    parser.add_argument("--out", default="_site")
    parser.add_argument("--built-at", default="", help="Build timestamp to stamp into data.json")
    args = parser.parse_args()

    try:
        registrations = load_distributions(Path(args.distributions_dir))
    except RegistryError as exc:
        sys.exit(f"error: {exc}")
    history = load_history(Path(args.history_dir)) if args.history_dir else {}
    metadata = load_metadata(Path(args.metadata_dir)) if args.metadata_dir else {}
    packages = build_packages(registrations, history, metadata)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = {"built_at": args.built_at or "unknown", "packages": packages}
    (out_dir / "data.json").write_text(json.dumps(data, indent=2))
    for asset in STATIC_ASSETS:
        shutil.copy2(SITE_DIR / asset, out_dir / asset)

    n_records = sum(len(v) for v in history.values())
    print(
        f"built {len(packages)} package(s), {n_records} history record(s) -> "
        f"{out_dir/'data.json'} (+ {', '.join(STATIC_ASSETS)})"
    )


if __name__ == "__main__":
    main()
