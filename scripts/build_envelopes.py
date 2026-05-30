#!/usr/bin/env python3
"""Construct append_history envelopes from a sweep matrix + validation artifacts.

The sweep workflow runs:

    discover  → matrix JSON (rows of (distro, package, ref))
       │
    validate  → per-row jobs that call autoware-index-github-actions'
                sweep-package.yaml, which resolves the Autoware version
                at runtime and uploads a result.json artifact named
                validate-result-<distro>-<package>-<resolved_version>
       │
    record    → THIS SCRIPT runs in the record job:
                globs for each row's artifact (one match per row),
                reads the resolved autoware_version out of result.json,
                and emits an envelope array for append_history.py.

A row is recorded as `pass` only when both build_outcome and test_outcome
are "success". Anything else is `fail`. Rows whose artifact is missing
(container init failed, sweep aborted) surface a ::error annotation and
are skipped — we cannot fabricate an autoware_version without it.

Inputs:
    --matrix-file PATH       JSON file: {"include": [matrix rows]}
    --results-dir DIR        Directory containing downloaded artifact subdirs
    --sweep-kind KIND        eager | release | nightly
    --actions-run-url URL    URL to the Actions run producing this record
    --output PATH            Where to write the envelope array
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ZERO_SHA = "0" * 40


def find_result(results_dir: Path, distro: str, package: str) -> dict | None:
    """Read the result.json that sweep-package.yaml uploaded for this row.

    Artifact name pattern (must match sweep-package.yaml):
        validate-result-<distro>-<package>-<resolved_version>
    Exactly one match is expected per sweep (one sweep-package run per
    package). Returns None if zero matches; raises if more than one.
    """
    matches = sorted(results_dir.glob(f"validate-result-{distro}-{package}-*"))
    if not matches:
        return None
    if len(matches) > 1:
        names = [m.name for m in matches]
        raise RuntimeError(
            f"ambiguous result artifacts for {distro}/{package}: {names}"
        )
    result_file = matches[0] / "result.json"
    if not result_file.is_file():
        return None
    return json.loads(result_file.read_text())


def envelope_for(row: dict, result: dict, sweep_kind: str, at: str, run_url: str) -> dict:
    distro = row["ros_distro"]
    package = row["package_name"]
    ref_kind = row["ref_kind"]
    ref_value = row["ref_value"]

    autoware_version = result["autoware_version"]
    resolved_sha = result.get("resolved_sha") or ZERO_SHA
    if len(resolved_sha) != 40:
        resolved_sha = ZERO_SHA
    build_ok = result.get("build_outcome") == "success"
    test_ok = result.get("test_outcome") == "success"
    status = "pass" if (build_ok and test_ok) else "fail"

    return {
        "ros_distro": distro,
        "package_name": package,
        "record": {
            "sweep_kind": sweep_kind,
            "ref_at_test": {"kind": ref_kind, "value": ref_value},
            "resolved_sha": resolved_sha,
            "autoware_version": autoware_version,
            "status": status,
            "at": at,
            "actions_run_url": run_url,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-file", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--sweep-kind", required=True, choices=["eager", "release", "nightly"])
    parser.add_argument("--actions-run-url", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    matrix = json.loads(Path(args.matrix_file).read_text())
    rows = matrix.get("include", [])
    results_dir = Path(args.results_dir)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    envelopes = []
    for row in rows:
        result = find_result(results_dir, row["ros_distro"], row["package_name"])
        if result is None:
            print(
                f"::error::no result artifact for "
                f"{row['ros_distro']}/{row['package_name']}; skipping envelope",
                file=sys.stderr,
            )
            continue
        envelopes.append(envelope_for(row, result, args.sweep_kind, now, args.actions_run_url))

    Path(args.output).write_text(json.dumps(envelopes, indent=2))
    print(f"wrote {len(envelopes)} envelope(s) to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
