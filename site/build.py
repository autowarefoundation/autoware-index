#!/usr/bin/env python3
"""Export the Autoware Index browse data and assemble the deployable site.

The browse site is a static front-end (index.html + styles.css + app.js) that
renders client-side from a generated data.json. This script is just the data
exporter + assembler: it joins the two branches the data lives on —

  - main:  distributions/<distro>.yaml   (what is registered)
  - data:  history/<distro>/<package>.ndjson   (how it has validated)

— summarizes each package, writes data.json, and copies the static assets next
to it so --out is a complete, deployable directory.

In CI the `data` branch is checked out into a sibling path and passed via
--history-dir; locally you can point --history-dir at site/sample-data/history
to preview a populated table before any real sweep has run. Because app.js
fetches data.json, preview over an HTTP server (fetch is blocked over file://):

    site/build.py --history-dir site/sample-data/history --out _site
    python -m http.server -d _site     # then open http://localhost:8000

Usage:
    site/build.py --distributions-dir distributions \\
                  --history-dir _data/history \\
                  --out _site
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import yaml

SITE_DIR = Path(__file__).resolve().parent
STATIC_ASSETS = ("index.html", "styles.css", "app.js")


def semver_key(version: str) -> tuple:
    """Sort key for an X.Y.Z SemVer string; non-numeric parts sort last."""
    parts = []
    for piece in str(version).split("."):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def load_distributions(distributions_dir: Path) -> list[dict]:
    """Flatten distributions/*.yaml into one registration record per package."""
    registrations: list[dict] = []
    for path in sorted(distributions_dir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        distro = doc.get("ros_distro") or path.stem
        for name, spec in (doc.get("packages") or {}).items():
            spec = spec or {}
            registrations.append(
                {
                    "distro": distro,
                    "name": name,
                    "repository": spec.get("repository", ""),
                    "governance": spec.get("governance", "community"),
                    "tags": spec.get("tags") or [],
                    "maintainers": spec.get("maintainers") or [],
                    "ref": spec.get("ref") or {},
                }
            )
    return registrations


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
        return {"current_status": "unknown", "last_green": None, "last_tested_at": None, "versions": []}

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
        for ver, rec in sorted(latest_per_version.items(), key=lambda kv: semver_key(kv[0]), reverse=True)
    ]

    return {
        "current_status": latest_overall.get("status", "unknown"),
        "last_green": last_green,
        "last_tested_at": latest_overall.get("at"),
        "versions": versions,
    }


def build_packages(registrations: list[dict], history: dict[tuple[str, str], list[dict]]) -> list[dict]:
    packages = []
    for reg in registrations:
        records = history.get((reg["distro"], reg["name"]), [])
        packages.append({**reg, **summarize(records)})
    packages.sort(key=lambda p: (p["name"], p["distro"]))
    return packages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distributions-dir", default="distributions")
    parser.add_argument("--history-dir", default="", help="Path to the data branch's history/ dir")
    parser.add_argument("--out", default="_site")
    parser.add_argument("--built-at", default="", help="Build timestamp to stamp into data.json")
    args = parser.parse_args()

    registrations = load_distributions(Path(args.distributions_dir))
    history = load_history(Path(args.history_dir)) if args.history_dir else {}
    packages = build_packages(registrations, history)

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
