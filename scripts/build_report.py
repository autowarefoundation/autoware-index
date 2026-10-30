#!/usr/bin/env python3
"""Write the build job summary and annotations for build-repository.yaml.

The build steps leave their state in .sweep-* files in the workspace. This
script reads them and says, for each registered package, whether it built and,
if not, why: its own build error, a failed or missing dependency in its
closure, an unresolvable rosdep key, or absence from the tree. The cause line
goes into a titled annotation, and an excerpt of the log goes into a
collapsed block under the summary table, so a reader does not need the raw log.

Package failures are data: the build steps exit 0 for them and only this step
goes red. When an earlier step failed instead, that is a pipeline fault with
no per-package verdicts; the summary names the step rather than blaming a
package.

Inputs (all relative to the working directory):
    .sweep-present.txt, .sweep-absent.txt   scan step
    .sweep-unresolved.txt, .sweep-rosdep.log rosdep step
    .sweep-build.log, log/latest_build/      build step
    .sweep-results.txt, .sweep-reasons.txt   verdict step

Usage:
    scripts/build_report.py --repo-name NAME --ref-kind KIND --ref-value REF \
        --steps '<toJSON(steps)>' [--logs-url URL] [--run-url URL]

Appends to $GITHUB_STEP_SUMMARY (or --summary). Exits 1 when a registered
package is absent or failed, else 0.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import html
import json
import os
from pathlib import Path
import re
import sys

ERROR_RE = re.compile(r"CMake Error|error:|undefined reference|fatal error")
CMAKE_NOT_FOUND_RE = re.compile(
    r'Could not find a package configuration file provided by\s+"([^"]+)"'
)

CONTEXT_LINES = 10
TAIL_LINES = 60
# GitHub rejects a step summary over 1 MiB, and shows at most 10 error
# annotations per step. Stay below both.
BLOCK_BYTES = 8 * 1024
SUMMARY_BYTES = 900 * 1024
MAX_ANNOTATIONS = 10

STEP_NAMES = {
    "clone": "Clone repository",
    "clone-dependencies": "Clone index dependencies",
    "closures": "Record index dependency closures",
    "resolve": "Resolve commit SHA",
    "scan": "Scan workspace for registered packages",
    "remove-exec-depend": "Remove exec_depend",
    "rosdep": "Install dependencies (rosdep)",
    "build": "Build (union of registered packages)",
    "verdict": "Derive per-package build verdicts",
}


@dataclass
class Cause:
    """What made one package fail: a one-line headline and a log excerpt."""

    headline: str
    excerpt: str = ""


def read_lines(path: Path) -> list[str]:
    return path.read_text(errors="replace").splitlines() if path.is_file() else []


def read_words(path: Path) -> list[str]:
    return [line.strip() for line in read_lines(path) if line.strip()]


def package_log(workspace: Path, pkg: str) -> list[str]:
    """Return the build output of one package.

    console_cohesion+ prints a package's whole output as one
    `--- output: <pkg>` block that ends with a bare `---`. colcon's own
    per-package log is the fallback, for a package that printed nothing there.
    """
    lines = read_lines(workspace / ".sweep-build.log")
    for marker in (f"--- output: {pkg}", f"--- stderr: {pkg}"):
        if marker in lines:
            start = lines.index(marker) + 1
            end = next((i for i in range(start, len(lines)) if lines[i] == "---"), len(lines))
            return lines[start:end]
    return read_lines(workspace / "log" / "latest_build" / pkg / "stdout_stderr.log")


def excerpt_of(lines: list[str]) -> str:
    """Return the error lines with CONTEXT_LINES after each, else the log tail."""
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if ERROR_RE.search(line):
            keep.update(range(i, min(i + CONTEXT_LINES + 1, len(lines))))
    if keep:
        picked, previous = [], None
        for i in sorted(keep):
            if previous is not None and i != previous + 1:
                picked.append("...")
            picked.append(lines[i])
            previous = i
    else:
        picked = lines[-TAIL_LINES:]
    return cap("\n".join(picked).strip("\n"), BLOCK_BYTES)


def headline_of(lines: list[str]) -> str | None:
    """Return a one-line cause: the first error line of the log."""
    for line in lines:
        missing = CMAKE_NOT_FOUND_RE.search(line)
        if missing:
            return f'CMake: package "{missing.group(1)}" not found'
    for i, line in enumerate(lines):
        if ERROR_RE.search(line):
            text = line.strip()
            # "CMake Error at CMakeLists.txt:16 (find_package):" says where, not
            # what; the indented paragraph after it says what.
            if text.endswith(":"):
                following = []
                for next_line in lines[i + 1 :]:
                    if not next_line.strip():
                        break
                    following.append(next_line.strip())
                text = " ".join([text, *following])
            return shorten(text, 200)
    return None


def cap(text: str, limit: int) -> str:
    data = text.encode()
    if len(data) <= limit:
        return text
    return data[:limit].decode(errors="ignore") + "\n... (cut, see the build logs artifact)"


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def own_cause(workspace: Path, pkg: str, unresolved: dict[str, list[str]]) -> Cause:
    """Why a package failed by itself, from rosdep or from its own build log."""
    if pkg in unresolved:
        keys = " ".join(unresolved[pkg])
        lines = [
            line
            for line in read_lines(workspace / ".sweep-rosdep.log")
            if line.startswith(f"{pkg}:")
        ]
        return Cause(f"rosdep cannot resolve key(s): {keys}", "\n".join(lines))
    lines = package_log(workspace, pkg)
    headline = headline_of(lines)
    if headline is None:
        headline = "build failed; no error line found, see the log tail"
    return Cause(headline, excerpt_of(lines) if lines else "")


def failure_cause(
    workspace: Path, pkg: str, kind: str, member: str, unresolved: dict[str, list[str]]
) -> Cause:
    """Turn one .sweep-reasons.txt line into a Cause."""
    if kind == "failed":
        cause = own_cause(workspace, member, unresolved)
        if member == pkg:
            return cause
        return Cause(f"blocked by `{member}`, which failed: {cause.headline}", cause.excerpt)
    if kind == "missing":
        return Cause(f"index dependency `{member}` is not in the imported source tree")
    if kind == "not-installed":
        lines = package_log(workspace, member)
        excerpt = excerpt_of(lines) if lines else ""
        if member == pkg:
            return Cause("no install directory after the build", excerpt)
        return Cause(f"dependency `{member}` has no install directory", excerpt)
    return Cause(f"unknown reason `{kind} {member}`")


def failed_steps(steps_json: str) -> list[str]:
    """Return the names of the steps that failed or were cancelled.

    `conclusion`, not `outcome`: a step with continue-on-error, such as the
    logs upload, does not take the verdicts with it.
    """
    try:
        steps = json.loads(steps_json or "{}")
    except json.JSONDecodeError:
        return []
    return [
        STEP_NAMES.get(step_id, step_id)
        for step_id, step in steps.items()
        if isinstance(step, dict) and step.get("conclusion") in ("failure", "cancelled")
    ]


def escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def cell(text: str) -> str:
    """Make compiler text safe for a table cell or a <summary>, e.g. `vector<int>`."""
    return html.escape(text, quote=False).replace("|", "\\|").replace("\n", " ")


def fence(text: str) -> str:
    """Return a code fence longer than any backtick run inside the text."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def report(
    workspace: Path,
    repo_name: str,
    ref_kind: str,
    ref_value: str,
    steps_json: str,
    logs_url: str,
    run_url: str,
) -> tuple[str, list[str], int]:
    """Return the summary Markdown, the annotation lines, and the exit code."""
    present = read_words(workspace / ".sweep-present.txt")
    absent = read_words(workspace / ".sweep-absent.txt")
    results = {}
    for line in read_lines(workspace / ".sweep-results.txt"):
        fields = line.split()
        if len(fields) >= 2:
            results[fields[0]] = fields[1]
    reasons = {}
    for line in read_lines(workspace / ".sweep-reasons.txt"):
        fields = line.split("\t")
        if len(fields) == 3:
            reasons.setdefault(fields[0], (fields[1], fields[2]))
    unresolved: dict[str, list[str]] = {}
    for line in read_lines(workspace / ".sweep-unresolved.txt"):
        fields = line.split(maxsplit=1)
        if len(fields) == 2:
            unresolved.setdefault(fields[0], []).append(fields[1])

    causes: dict[str, Cause] = {}
    for pkg in present:
        if results.get(pkg) == "failure":
            kind, member = reasons.get(pkg, ("failed", pkg))
            causes[pkg] = failure_cause(workspace, pkg, kind, member, unresolved)
    for pkg in absent:
        causes[pkg] = Cause("not found in the repository tree at this ref")

    total = len(present) + len(absent)
    out = [f"### build {repo_name} @ {ref_kind} {ref_value}", ""]
    if causes:
        listed = []
        for pkg, cause in causes.items():
            if sum(len(entry) for entry in listed) > 1000:
                listed.append(f"and {len(causes) - len(listed)} more")
                break
            listed.append(f"{pkg} ({cause.headline})")
        out += [f"**{len(causes)} of {total} packages failed:** {cell(', '.join(listed))}", ""]
    broken_steps = failed_steps(steps_json)
    if broken_steps:
        names = ", ".join(f"`{name}`" for name in broken_steps)
        out += [
            f"> [!WARNING]\n> The step {names} did not finish. This is a pipeline fault, not a package"
            " fault, so packages without a verdict are inconclusive."
            + (f" See the [run log]({run_url})." if run_url else ""),
            "",
        ]
    no_verdict = [pkg for pkg in present if pkg not in results]
    if no_verdict:
        out += [f"**{len(no_verdict)} of {total} packages have no verdict.**", ""]
    elif total and not causes:
        out += [f"**All {total} packages built.**", ""]

    out += ["| package | present | build | reason |", "|---|---|---|---|"]
    for pkg in present:
        build = results.get(pkg, "-")
        reason = causes[pkg].headline if pkg in causes else ""
        if build == "-":
            reason = "inconclusive: no verdict"
        out.append(f"| {pkg} | yes | {build} | {cell(reason)} |")
    for pkg in absent:
        out.append(f"| {pkg} | **no** | - | {cell(causes[pkg].headline)} |")
    out.append("")

    size = sum(len(line.encode()) + 1 for line in out)
    for pkg, cause in causes.items():
        if not cause.excerpt:
            continue
        marker = fence(cause.excerpt)
        block = (
            f"<details><summary>{pkg}: {cell(shorten(cause.headline, 200))}</summary>\n\n"
            f"{marker}text\n{cause.excerpt}\n{marker}\n\n</details>\n"
        )
        if size + len(block.encode()) > SUMMARY_BYTES:
            out.append("More logs are left out to keep the summary under the size limit.\n")
            break
        out.append(block)
        size += len(block.encode())
    if causes and logs_url:
        out.append(f"Full logs: [build logs artifact]({logs_url})\n")

    annotations = []
    for pkg, cause in causes.items():
        if len(annotations) == MAX_ANNOTATIONS - 1 and len(causes) > MAX_ANNOTATIONS:
            more = len(causes) - len(annotations)
            annotations.append(f"::error::{more} more packages failed; see the job summary")
            break
        title = f"{pkg} build failed" if pkg in present else f"{pkg} not found"
        annotations.append(f"::error title={escape_property(title)}::{escape_data(cause.headline)}")

    return "\n".join(out) + "\n", annotations, 1 if causes else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-name", required=True)
    parser.add_argument("--ref-kind", required=True)
    parser.add_argument("--ref-value", required=True)
    parser.add_argument("--steps", default="{}", help="toJSON(steps) of the build job")
    parser.add_argument("--logs-url", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args()

    summary, annotations, code = report(
        Path.cwd(),
        args.repo_name,
        args.ref_kind,
        args.ref_value,
        args.steps,
        args.logs_url,
        args.run_url,
    )
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
