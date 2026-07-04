#!/usr/bin/env python3
"""Apply a "register packages" issue to distributions/<distro>.yaml.

The register workflow (.github/workflows/register.yaml) runs this over the
body of an issue created from the register-package.yml issue form (normally
pre-filled by the browse site's register page). It turns the form's fields
into a registry edit the same way a careful human would — append one entry
under `repositories:` — but programmatically, so the indentation accidents
that plague pasting nested YAML into web editors are structurally impossible.

Contract with the issue form (field labels are the section headings GitHub
renders into the issue body):

  ### ROS distro                    -> which distributions/<distro>.yaml
  ### Registry entry                -> ONE repositories entry, as YAML
                                       (the form renders it fenced; any
                                       consistent base indentation is fine)
  ### Developer Certificate of Origin -> the required, ticked checkbox

The entry is parsed structurally (never spliced as text) and re-emitted in
the registry's house style, inserted at the end of the `repositories:` block
(wherever that block sits in the file). After insertion the file is
re-parsed and the entry compared for deep equality — a failed round trip is
a hard error, never a silently mangled registry.

Only structural validation happens here (parseable entry, exactly one entry,
valid entry name, distro file exists, name not already registered — a
duplicate YAML key would silently shadow the original). The semantic gates
(schema conformance, tag vocabulary, URL/package uniqueness, placeholder
maintainers, ref resolution) run in the workflow and on the resulting PR,
exactly as for a hand-written registration.

Usage:
    scripts/apply_registration.py --issue-body body.md \
        [--distributions-dir distributions]

On success the modified file is written and `distro=<distro>` and
`name=<entry name>` are printed to stdout (the workflow appends them to
$GITHUB_OUTPUT). On any problem: diagnostics to stderr, exit 1, file
untouched.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

import yaml

HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^```[\w-]*\s*$")
ENTRY_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
DISTRO_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# A top-level key (or top-level comment) ends the `repositories:` block.
TOP_LEVEL_RE = re.compile(r"^[^\s#]")
REPOSITORIES_LINE_RE = re.compile(r"^repositories:\s*(\{\s*\}\s*)?(#.*)?$")

SECTION_DISTRO = "ros distro"
SECTION_ENTRY = "registry entry"
SECTION_DCO = "developer certificate of origin"


class RegistrationError(Exception):
    """A problem with the issue content or the target registry file."""


def split_sections(body: str) -> dict[str, str]:
    """Issue-form bodies render one `### <field label>` heading per field."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
        match = HEADING_RE.match(line)
        if match:
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = match.group(1).lower()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def strip_fence(text: str) -> str:
    """Drop the ```yaml fence the issue form's `render: yaml` adds."""
    lines = text.splitlines()
    if lines and FENCE_RE.match(lines[0]):
        lines = lines[1:]
        if lines and FENCE_RE.match(lines[-1]):
            lines = lines[:-1]
    return "\n".join(lines)


def dedent_common(text: str) -> str:
    """Strip the largest whitespace prefix shared by every non-empty line."""
    lines = text.splitlines()
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    cut = min(indents) if indents else 0
    return "\n".join(line[cut:] if line.strip() else "" for line in lines)


def parse_entry(text: str) -> tuple[str, dict]:
    """Parse the entry section into (entry name, spec mapping), structurally checked."""
    cleaned = dedent_common(strip_fence(text)).strip("\n")
    if not cleaned.strip():
        raise RegistrationError("the registry entry is empty")
    try:
        doc = yaml.safe_load(cleaned)
    except yaml.YAMLError as exc:
        raise RegistrationError(
            f"the registry entry is not valid YAML: {exc}\n"
            "Re-copy it from the register page and submit again."
        ) from exc
    if not isinstance(doc, dict) or not doc:
        raise RegistrationError(
            f"the registry entry must be a YAML mapping of one repository entry, "
            f"got {type(doc).__name__}"
        )
    if len(doc) != 1:
        raise RegistrationError(
            f"the registry entry must contain exactly ONE repository entry, "
            f"got {len(doc)}: {', '.join(map(repr, doc))}"
        )
    ((name, spec),) = doc.items()
    if not isinstance(name, str) or not ENTRY_NAME_RE.match(name):
        raise RegistrationError(
            f"entry name {name!r} must match {ENTRY_NAME_RE.pattern} "
            '(quote names YAML would type, e.g. "no")'
        )
    if not isinstance(spec, dict) or not spec:
        raise RegistrationError(f"entry {name!r} must map to the repository fields, got {spec!r}")
    return name, spec


class _IndentDumper(yaml.SafeDumper):
    """SafeDumper that indents block sequences (the registry's house style)."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


def emit_entry(name: str, spec: dict) -> str:
    """Re-emit the parsed entry, indented for the `repositories:` block."""
    dumped = yaml.dump(
        {name: spec},
        Dumper=_IndentDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=88,
    )
    return "".join(f"  {line}\n" if line.strip() else "\n" for line in dumped.splitlines())


def insert_entry(path: Path, name: str, spec: dict) -> None:
    """Insert the entry at the end of the file's `repositories:` block."""
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistrationError(f"{path}: cannot parse the current registry file: {exc}") from exc
    if not isinstance(doc, dict) or "repositories" not in doc:
        raise RegistrationError(f"{path}: no `repositories:` block to register into")
    existing = doc.get("repositories") or {}
    if name in existing:
        raise RegistrationError(
            f"{path}: {name!r} is already a repository entry — a second YAML key "
            "would silently shadow it; update the existing entry instead"
        )

    lines = text.splitlines(keepends=True)
    rep_index = next((i for i, line in enumerate(lines) if REPOSITORIES_LINE_RE.match(line)), None)
    if rep_index is None:
        raise RegistrationError(f"{path}: could not locate the top-level `repositories:` line")
    if "{" in lines[rep_index]:  # `repositories: {}` -> open the block form
        lines[rep_index] = "repositories:\n"

    boundary = next(
        (i for i in range(rep_index + 1, len(lines)) if TOP_LEVEL_RE.match(lines[i])),
        len(lines),
    )
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines[boundary:boundary] = [emit_entry(name, spec)]
    new_text = "".join(lines)

    # Round-trip tripwire: the file must still parse and must contain exactly
    # the entry that was requested — never a silently mangled registry.
    reparsed = yaml.safe_load(new_text)
    if not isinstance(reparsed, dict) or reparsed.get("repositories", {}).get(name) != spec:
        raise RegistrationError(
            f"{path}: inserting {name!r} did not round-trip cleanly; refusing to write"
        )
    path.write_text(new_text, encoding="utf-8")


def apply(body: str, distributions_dir: Path) -> tuple[str, str]:
    """Full pipeline: issue body -> edited distro file -> (distro, name)."""
    sections = split_sections(body)

    # The dropdown section is a bare word ("jazzy"); an unanswered form
    # renders "_No response_", which correctly fails the pattern.
    distro_words = sections.get(SECTION_DISTRO, "").split()
    distro = distro_words[0].lower() if distro_words else ""
    if not DISTRO_RE.match(distro):
        raise RegistrationError(f"missing or invalid ROS distro {distro!r}")

    dco = sections.get(SECTION_DCO) or ""
    if "[x]" not in dco.lower():
        raise RegistrationError(
            "the Developer Certificate of Origin box is not ticked — "
            "edit the issue and tick it to certify the registration"
        )

    if SECTION_ENTRY not in sections:
        raise RegistrationError("the issue has no 'Registry entry' section")
    name, spec = parse_entry(sections[SECTION_ENTRY])

    path = distributions_dir / f"{distro}.yaml"
    if not path.is_file():
        raise RegistrationError(f"no registry file for distro {distro!r} ({path} does not exist)")
    insert_entry(path, name, spec)
    return distro, name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-body", required=True, help="file containing the issue body")
    parser.add_argument("--distributions-dir", default="distributions")
    args = parser.parse_args()

    body = Path(args.issue_body).read_text(encoding="utf-8")
    try:
        distro, name = apply(body, Path(args.distributions_dir))
    except RegistrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"distro={distro}")
    print(f"name={name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
