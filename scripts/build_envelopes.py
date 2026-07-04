#!/usr/bin/env python3
"""Fan a repository-sweep's artifacts out into per-package history envelopes.

The sweep workflow runs:

    discover  → matrix JSON (one row per (distro, repository), with the
                space-separated registered package names)
       │
    validate  → per-row jobs that call autoware-index-github-actions'
                sweep-repository.yaml: ONE clone + union build per repo,
                per-package verdicts derived inside the job, uploaded as
                validate-result-<distro>-<repo_name>-<version> containing
                result.json (schema 2) + package-xmls/<pkg>.xml
       │
    record    → THIS SCRIPT runs in the record job:
                matches each row's artifact by result.json CONTENT (the
                on-disk artifact layout is not stable), fans it out into one
                envelope per registered package, stages the per-package
                state/metadata side-outputs, and hands everything to
                append_history.py for a single data-branch commit.

Honesty rules (locked decision 6, applied per package):
  - pass  only when the package's own closure build AND its own tests
    both report "success" in result.json;
  - fail  only when either reports an actual "failure";
  - anything else (package absent from the tree with present:false, null
    outcomes from a cancelled/half-run job, a missing artifact) is
    INCONCLUSIVE: a ::error annotation and a skipped envelope, never a
    fabricated record.
Every record is validated against schema/history-record.schema.json
(record schema 2) before it is emitted.

Side-outputs for append_history.py:
  --states-output    state/<distro>/<repo>.json payloads, emitted ONLY for
                     rows where EVERY registered package got a conclusive
                     record, so the level-triggered discover re-sweeps any
                     row that recorded partially or not at all.
  --metadata-output  staged metadata/<distro>/<pkg>.xml files copied from the
                     artifacts' package-xmls/ (pristine upstream package.xml
                     per present package, cached for the site's descriptions).

Infrastructure honesty (locked decision 5 clarification): a non-empty matrix
that yields ZERO envelopes is a pipeline fault, not a package failure, so this
script exits non-zero and the record job goes loudly red instead of green-on-
nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import shutil
import sys

import jsonschema

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "history-record.schema.json"


def status_for(outcome: dict) -> str | None:
    """Map one package's sweep outcomes to pass/fail, or None if inconclusive.

    `outcome` is result.json's packages.<name> object: {present, build_outcome,
    test_outcome}. present:false or null outcomes mean nothing was validated
    for this package; recording either pass (false green) or fail (false red)
    would be a lie, so the caller skips the record loudly instead.
    """
    if not outcome.get("present"):
        return None
    build = outcome.get("build_outcome")
    test = outcome.get("test_outcome")
    if build == "success" and test == "success":
        return "pass"
    if build == "failure" or test == "failure":
        return "fail"
    return None


def find_result(results_dir: Path, distro: str, repo_name: str) -> Path | None:
    """Find the result.json sweep-repository.yaml uploaded for (distro, repo).

    download-artifact's on-disk layout is not stable: with several matching
    artifacts it makes a per-artifact subdirectory, but with a single match it
    extracts straight into the download path root. So we cannot key off the
    artifact directory name. Every result.json carries its own identity, so
    search recursively and match on the file's contents. Returns the PATH (the
    sibling package-xmls/ dir is needed too), not the parsed payload.
    """
    for result_file in sorted(results_dir.glob("**/result.json")):
        try:
            data = json.loads(result_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if (
            data.get("schema") == 2
            and data.get("ros_distro") == distro
            and data.get("repo_name") == repo_name
        ):
            return result_file
    return None


def envelopes_for_row(
    row: dict, result: dict, sweep_kind: str, at: str, run_url: str
) -> tuple[list[dict], list[str]]:
    """Build the per-package envelopes for one matrix row + its result.json.

    Returns (envelopes, skip_reasons). A row is fully conclusive when
    len(envelopes) == number of registered packages; only then may its
    state file advance.
    """
    distro = row["ros_distro"]
    packages = row["packages"].split()
    skips: list[str] = []

    autoware_version = result.get("autoware_version")
    if not autoware_version:
        return [], [
            f"{distro}/{row['repo_name']}: result.json has no autoware_version (resolve did not complete)"
        ]

    resolved_sha = result.get("resolved_sha") or ""
    if not SHA_RE.match(resolved_sha):
        # No real sha means the clone/resolve never completed: nothing was
        # validated. Substituting a sentinel would fabricate provenance inside
        # otherwise-conclusive records; skip the whole row loudly instead.
        return [], [
            f"{distro}/{row['repo_name']}: result.json has no valid resolved_sha "
            f"({resolved_sha!r}); nothing was validated"
        ]

    outcomes = result.get("packages") or {}
    envelopes: list[dict] = []
    for package in packages:
        outcome = outcomes.get(package)
        if outcome is None:
            skips.append(f"{distro}/{package}: result.json carries no outcome for this package")
            continue
        status = status_for(outcome)
        if status is None:
            skips.append(
                f"{distro}/{package}: inconclusive (present={outcome.get('present')!r}, "
                f"build={outcome.get('build_outcome')!r}, test={outcome.get('test_outcome')!r}); "
                f"nothing was validated"
            )
            continue
        envelopes.append(
            {
                "ros_distro": distro,
                "package_name": package,
                "record": {
                    "schema": 2,
                    "sweep_kind": sweep_kind,
                    "ref_at_test": {"kind": row["ref_kind"], "value": row["ref_value"]},
                    "resolved_sha": resolved_sha,
                    "autoware_version": autoware_version,
                    "status": status,
                    "at": at,
                    "actions_run_url": run_url,
                    "repository": row["repository"],
                    "repo_name": row["repo_name"],
                },
            }
        )
    return envelopes, skips


def stage_metadata(result_file: Path, row: dict, out_dir: Path) -> int:
    """Copy the artifact's package-xmls/<pkg>.xml into out_dir/<distro>/.

    Descriptions are orthogonal to pass/fail: every PRESENT package's pristine
    package.xml is cached, whatever its verdict.
    """
    staged = 0
    xml_dir = result_file.parent / "package-xmls"
    target_dir = out_dir / row["ros_distro"]
    for package in row["packages"].split():
        source = xml_dir / f"{package}.xml"
        if source.is_file():
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target_dir / f"{package}.xml")
            staged += 1
    return staged


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-file", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--sweep-kind", required=True, choices=["eager", "nightly"])
    parser.add_argument("--actions-run-url", required=True)
    parser.add_argument("--output", required=True, help="Where to write the envelope array")
    parser.add_argument(
        "--states-output", required=True, help="Where to write the state-advance array"
    )
    parser.add_argument(
        "--metadata-output", required=True, help="Dir to stage metadata/<distro>/<pkg>.xml files"
    )
    args = parser.parse_args()

    matrix = json.loads(Path(args.matrix_file).read_text())
    rows = matrix.get("include", [])
    results_dir = Path(args.results_dir)
    metadata_dir = Path(args.metadata_output)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))

    all_envelopes: list[dict] = []
    states: list[dict] = []
    for row in rows:
        distro, repo_name = row["ros_distro"], row["repo_name"]
        result_file = find_result(results_dir, distro, repo_name)
        if result_file is None:
            print(
                f"::error::no result artifact for {distro}/{repo_name}; skipping its envelopes",
                file=sys.stderr,
            )
            continue
        result = json.loads(result_file.read_text())

        envelopes, skips = envelopes_for_row(
            row, result, args.sweep_kind, now, args.actions_run_url
        )
        for reason in skips:
            print(f"::error::{reason}; skipping envelope", file=sys.stderr)

        valid: list[dict] = []
        for envelope in envelopes:
            schema_errors = sorted(validator.iter_errors(envelope["record"]), key=str)
            if schema_errors:
                for err in schema_errors:
                    print(
                        f"::error::{distro}/{envelope['package_name']}: history record fails schema: {err.message}",
                        file=sys.stderr,
                    )
                continue
            valid.append(envelope)

        all_envelopes.extend(valid)
        stage_metadata(result_file, row, metadata_dir)

        registered = row["packages"].split()
        if len(valid) == len(registered):
            # Every registered package recorded conclusively: the state file
            # may advance, so the level-triggered discover stops re-sweeping
            # this row. Partial rows stay stale on purpose: they re-sweep
            # (and re-annotate) until the registry or the pipeline is fixed.
            states.append(
                {
                    "ros_distro": distro,
                    "repo_name": repo_name,
                    "state": {
                        "url": row["repository"],
                        "ref": {"kind": row["ref_kind"], "value": row["ref_value"]},
                        "packages": sorted(registered),
                        "last_run_url": args.actions_run_url,
                        "at": now,
                    },
                }
            )
        else:
            print(
                f"::warning::{distro}/{repo_name}: {len(valid)}/{len(registered)} package(s) "
                f"recorded conclusively; state not advanced, so the row re-sweeps until conclusive",
                file=sys.stderr,
            )

    Path(args.output).write_text(json.dumps(all_envelopes, indent=2))
    Path(args.states_output).write_text(json.dumps(states, indent=2))
    print(
        f"wrote {len(all_envelopes)} envelope(s) to {args.output}, "
        f"{len(states)} state advance(s) to {args.states_output}",
        file=sys.stderr,
    )

    if rows and not all_envelopes:
        sys.exit(
            "::error::a non-empty sweep matrix produced ZERO envelopes: this is a pipeline "
            "fault (missing artifacts or wholly inconclusive results), not a package failure; "
            "failing the record job loudly instead of recording nothing in silence"
        )


if __name__ == "__main__":
    main()
