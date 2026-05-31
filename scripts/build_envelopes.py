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
are "success", and `fail` when either actually "failure". Rows that are
inconclusive (artifact missing, no autoware_version, or both steps skipped
so nothing was validated) surface a ::error annotation and are skipped — we
do not fabricate a record. Every constructed record is validated against
schema/history-record.schema.json before it is emitted.

Inputs:
    --matrix-file PATH       JSON file: {"include": [matrix rows]}
    --results-dir DIR        Directory containing downloaded artifact subdirs
    --sweep-kind KIND        eager | nightly
    --actions-run-url URL    URL to the Actions run producing this record
    --output PATH            Where to write the envelope array
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import jsonschema

ZERO_SHA = "0" * 40
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "history-record.schema.json"


def status_for(result: dict) -> str | None:
    """Map a sweep's build/test outcomes to a pass/fail status.

    GitHub step outcomes are one of success/failure/skipped/cancelled/''.
    Only a clean success on BOTH steps is `pass`; an actual `failure` on
    either is `fail`. Anything else (both steps skipped because the ref had
    no self-packages, a cancelled run, empty outcomes) is INCONCLUSIVE: the
    sweep validated nothing, so recording either pass (false green — worse,
    consumers trust an untested package) or fail (false red) would be a lie.
    Returns None for inconclusive so the caller skips the record loudly
    rather than fabricating one.
    """
    build = result.get("build_outcome")
    test = result.get("test_outcome")
    if build == "success" and test == "success":
        return "pass"
    if build == "failure" or test == "failure":
        return "fail"
    return None


def find_result(results_dir: Path, distro: str, package: str) -> dict | None:
    """Find the result.json sweep-package.yaml uploaded for (distro, package).

    download-artifact's on-disk layout is not stable: with several matching
    artifacts it makes a per-artifact subdirectory, but with a single match it
    extracts straight into the download path root. So we cannot key off the
    artifact directory name. Every result.json carries its own ros_distro and
    package_name, so search recursively and match on the file's contents.
    """
    for result_file in sorted(results_dir.glob("**/result.json")):
        try:
            data = json.loads(result_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("ros_distro") == distro and data.get("package_name") == package:
            return data
    return None


def envelope_for(
    row: dict, result: dict, sweep_kind: str, at: str, run_url: str
) -> tuple[dict | None, str | None]:
    """Build one append_history envelope from a matrix row + its result.json.

    Returns (envelope, None) on success, or (None, reason) when the row is
    inconclusive and must be skipped (missing autoware_version, or build/test
    outcomes that validated nothing).
    """
    distro = row["ros_distro"]
    package = row["package_name"]
    ref_kind = row["ref_kind"]
    ref_value = row["ref_value"]

    autoware_version = result.get("autoware_version")
    if not autoware_version:
        return None, "result.json has no autoware_version (resolve job did not complete)"

    status = status_for(result)
    if status is None:
        return None, (
            f"inconclusive outcomes (build={result.get('build_outcome')!r}, "
            f"test={result.get('test_outcome')!r}); nothing was validated"
        )

    resolved_sha = result.get("resolved_sha") or ZERO_SHA
    if len(resolved_sha) != 40:
        resolved_sha = ZERO_SHA

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
    }, None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-file", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--sweep-kind", required=True, choices=["eager", "nightly"])
    parser.add_argument("--actions-run-url", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    matrix = json.loads(Path(args.matrix_file).read_text())
    rows = matrix.get("include", [])
    results_dir = Path(args.results_dir)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))

    envelopes = []
    for row in rows:
        distro, package = row["ros_distro"], row["package_name"]
        result = find_result(results_dir, distro, package)
        if result is None:
            print(
                f"::error::no result artifact for {distro}/{package}; skipping envelope",
                file=sys.stderr,
            )
            continue

        envelope, reason = envelope_for(row, result, args.sweep_kind, now, args.actions_run_url)
        if envelope is None:
            print(f"::error::{distro}/{package}: {reason}; skipping envelope", file=sys.stderr)
            continue

        schema_errors = sorted(validator.iter_errors(envelope["record"]), key=str)
        if schema_errors:
            for err in schema_errors:
                print(
                    f"::error::{distro}/{package}: history record fails schema: {err.message}",
                    file=sys.stderr,
                )
            continue

        envelopes.append(envelope)

    Path(args.output).write_text(json.dumps(envelopes, indent=2))
    print(f"wrote {len(envelopes)} envelope(s) to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
