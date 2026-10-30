#!/usr/bin/env python3
"""Write the test job summary and annotations for test-repository.yaml.

The test job runs `colcon test` once per successfully built package and
records only success or failure in _result/result.json. This script adds the
why: the failed test cases and their first failure message, read from the
JUnit XML files that colcon test leaves under build/<package>/. A package that
failed without a failed case in its XML (a crash, a timeout, a missing result
file) gets the tail of its colcon test log instead.

The shared helpers (escaping, caps, step names) live in build_report.py, so
the build and test summaries read the same way.

Usage:
    scripts/test_report.py --steps '<toJSON(steps)>' [--logs-url URL] [--run-url URL]

Appends to $GITHUB_STEP_SUMMARY (or --summary). Exits 1 when a built package
has no successful test outcome, else 0.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ElementTree

from build_report import BLOCK_BYTES
from build_report import Cause
from build_report import MAX_ANNOTATIONS
from build_report import SUMMARY_BYTES
from build_report import TAIL_LINES
from build_report import cap
from build_report import cell
from build_report import escape_data
from build_report import escape_property
from build_report import failed_steps
from build_report import fence
from build_report import inline
from build_report import read_lines
from build_report import shorten

CASE_LINES = 30
MAX_CASES_LISTED = 5
# A JUnit file larger than this is not parsed; its package falls back to the log.
MAX_XML_BYTES = 10 * 1024 * 1024

STEP_NAMES = {
    "download": "Download build result",
    "select": "Select successfully built packages",
    "cache": "Restore built workspace",
    "verify": "Verify restored build",
    "install": "Install test workspace dependencies",
    "relax": "Relax the CycloneDDS socket buffer minimum",
    "tests": "Run package tests",
    "upload": "Upload result",
}


@dataclass
class FailedCase:
    name: str
    message: str
    details: str


def junit_cases(build_dir: Path) -> tuple[int, list[FailedCase]]:
    """Return the count of test cases and the failed ones under build_dir.

    Any *.xml whose root is <testsuite> or <testsuites> counts, as for
    `colcon test-result`: gtest, pytest, and the ament linters all write one.
    """
    total, failed = 0, []
    for path in sorted(build_dir.rglob("*.xml")) if build_dir.is_dir() else []:
        try:
            if path.stat().st_size > MAX_XML_BYTES:
                continue
            root = ElementTree.parse(path).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        if root.tag not in ("testsuite", "testsuites"):
            continue
        for case in root.iter("testcase"):
            total += 1
            problem = next((c for c in case if c.tag in ("failure", "error")), None)
            if problem is None:
                continue
            classname = case.get("classname") or ""
            name = case.get("name") or "?"
            full = f"{classname}.{name}" if classname else name
            failed.append(
                FailedCase(full, problem.get("message") or "", (problem.text or "").strip("\n"))
            )
    return total, failed


def package_test_log(workspace: Path, pkg: str) -> list[str]:
    """Return the newest colcon test log of one package.

    The job runs colcon test once per package, so log/latest_test holds only
    the last package; search every log/test_* run instead.
    """
    logs = sorted(workspace.glob(f"log/test_*/{pkg}/stdout_stderr.log"))
    return read_lines(logs[-1]) if logs else []


def package_test_cause(workspace: Path, pkg: str) -> Cause:
    total, failed = junit_cases(workspace / "build" / pkg)
    if failed:
        names = ", ".join(f"`{case.name}`" for case in failed[:MAX_CASES_LISTED])
        more = len(failed) - MAX_CASES_LISTED
        headline = f"{len(failed)} of {total} test cases failed: {names}"
        if more > 0:
            headline += f" and {more} more"
        # gtest repeats the message in the element text; pytest puts the
        # traceback there. Use the message for the headline, the text below.
        first = ((failed[0].message or failed[0].details).splitlines() or [""])[0]
        if first:
            headline += f" (first: {shorten(first, 150)})"
        blocks = []
        for case in failed:
            lines = (case.details or case.message).splitlines()[:CASE_LINES]
            blocks.append("\n".join([case.name, *("  " + line for line in lines)]))
        return Cause(headline, cap("\n\n".join(blocks), BLOCK_BYTES))
    lines = package_test_log(workspace, pkg)
    return Cause(
        "tests failed, but no failed case is in the JUnit results; see the log tail",
        cap("\n".join(lines[-TAIL_LINES:]), BLOCK_BYTES),
    )


def report(
    workspace: Path, steps_json: str, logs_url: str, run_url: str
) -> tuple[str, list[str], int]:
    """Return the summary Markdown, the annotation lines, and the exit code."""
    result = json.loads((workspace / "_result" / "result.json").read_text())
    rows, causes, inconclusive = [], {}, []
    for pkg, outcome in result["packages"].items():
        if not (outcome["present"] and outcome["build_outcome"] == "success"):
            rows.append((pkg, "skipped (not built)", ""))
            continue
        status = outcome["test_outcome"]
        if status == "failure":
            causes[pkg] = package_test_cause(workspace, pkg)
            rows.append((pkg, "failure", causes[pkg].headline))
        elif status == "success":
            rows.append((pkg, "success", ""))
        else:
            inconclusive.append(pkg)
            rows.append((pkg, "inconclusive", "no test outcome"))

    tested = sum(1 for _, status, _ in rows if status != "skipped (not built)")
    out = [f"### test {result['repo_name']}", ""]
    if causes:
        listed = ", ".join(causes)
        out += [f"**{len(causes)} of {tested} tested packages failed:** {cell(listed)}", ""]
    broken = failed_steps(steps_json, STEP_NAMES)
    if broken:
        out += [
            f"> [!WARNING]\n> The step {', '.join(broken)} did not finish. This is not a"
            " package fault, so packages without a test outcome are inconclusive."
            + (f" See the [run log]({run_url})." if run_url else ""),
            "",
        ]
    if inconclusive:
        out += [f"**{len(inconclusive)} of {tested} tested packages have no outcome.**", ""]
    elif tested and not causes:
        out += [f"**All {tested} tested packages passed.**", ""]

    out += ["| package | test | reason |", "|---|---|---|"]
    out += [f"| {pkg} | {status} | {cell(reason)} |" for pkg, status, reason in rows]
    out.append("")

    size = sum(len(line.encode()) + 1 for line in out)
    for pkg, cause in causes.items():
        if not cause.excerpt:
            continue
        marker = fence(cause.excerpt)
        block = (
            f"<details><summary>{pkg}: {inline(shorten(cause.headline, 200))}</summary>\n\n"
            f"{marker}text\n{cause.excerpt}\n{marker}\n\n</details>\n"
        )
        if size + len(block.encode()) > SUMMARY_BYTES:
            out.append("More logs are left out to keep the summary under the size limit.\n")
            break
        out.append(block)
        size += len(block.encode())
    if logs_url:
        out.append(f"Full logs: [test logs artifact]({logs_url})\n")

    annotations = []
    for pkg, cause in causes.items():
        if len(annotations) == MAX_ANNOTATIONS - 1 and len(causes) > MAX_ANNOTATIONS:
            more = len(causes) - len(annotations)
            annotations.append(f"::error::{more} more packages failed tests; see the job summary")
            break
        title = escape_property(f"{pkg} tests failed")
        annotations.append(f"::error title={title}::{escape_data(cause.headline)}")

    return "\n".join(out) + "\n", annotations, 1 if causes or inconclusive else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", default="{}", help="toJSON(steps) of the test job")
    parser.add_argument("--logs-url", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args()

    summary, annotations, code = report(Path.cwd(), args.steps, args.logs_url, args.run_url)
    for line in annotations:
        print(line)
    if args.summary:
        with open(args.summary, "a") as handle:
            handle.write(summary)
    else:
        sys.stdout.write(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
