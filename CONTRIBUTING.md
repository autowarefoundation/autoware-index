# Contributing to autoware-index

This repository is the **source of truth** for which community Autoware packages
exist and where their source lives. The primary contribution is **registering
your repository's packages** (or updating an existing registration). General
Autoware contribution guidelines apply too — see
<https://autowarefoundation.github.io/autoware-documentation/main/contributing/>.

## Registering or updating packages

The [register page](https://autowarefoundation.github.io/autoware-index/register.html)
on the browse site walks you through this: it writes the entry, checks it
against the same rules the `validate` workflow enforces, and hands you a
pre-filled registration request whose submission opens the pull request for
you (the `register` workflow applies the entry and credits you as the
author). The manual flow it automates:

1. **Fork** this repository.
2. Edit `distributions/<distro>.yaml` for each ROS distro you support — add or
   update ONE entry under `repositories:` for your repository, listing every
   ROS package you register under its `packages:` map. The full field
   reference is in [`README.md`](README.md#schema-reference); the
   machine-checkable contract is
   [`schema/distribution.schema.json`](schema/distribution.schema.json).
3. **Validate locally** before opening a PR (see below).
4. Open a pull request. The `validate` workflow runs automatically, and the
   `build-check` workflow builds + tests the packages of every entry whose
   url, ref, or package set your PR adds or changes against the current
   Autoware release; metadata-only edits (tags, descriptions, maintainers,
   governance) skip the build. It is advisory — it informs the review rather
   than hard-blocking the merge. A maintainer reviews and merges.

After merge, the **eager sweep** clones your repository once at its registered
`ref`, validates every registered package against the current latest published
Autoware release, and appends one result per package to the
[`data`](../../tree/data) branch; `kind: branch` refs are additionally re-swept
nightly. The [browse site](README.md) renders the resulting per-package
compatibility tables.

## Registering a monorepo (several packages, one repository)

A repository hosting several ROS packages is **one** `repositories:` entry:

- The entry's key is the repository's name — pure registry identity, never
  looked up in the source tree. The `packages:` keys are different: each must
  equal a real `package.xml` `<name>` at the registered ref, because the
  sweep resolves every one with `colcon list` and fails loudly when a name is
  absent. Don't list the repository's name under `packages:` unless a package
  of that exact name actually exists.
- All packages of the repository share the entry's single `ref` (lockstep):
  one repo @ one sha is one source state, and `vcs import` can only
  materialize one checkout. A package that needs a different ref than its
  siblings must move to its own repository.
- You don't have to register every package the repository contains — but be
  aware that consumers clone the WHOLE repository, so unregistered sibling
  packages ship alongside registered ones without any validation claims.
  Consumers scope their builds with `colcon build --packages-up-to
<registered names>`.
- Each package gets its own independent history record per sweep: a sibling's
  test failure never marks your package red (a broken in-repo _dependency_
  does — your package would not build).

## What the validation enforces

A PR cannot merge unless, for every changed entry:

- the file conforms to `schema/distribution.schema.json` (schema_version "2");
- `ros_distro` equals the filename stem;
- the registered `ref` **actually resolves** in the named repository
  (`git ls-remote`) — a `tag`/`branch` that does not exist is rejected;
- no two entries register the same repository URL (spelling variants like a
  `.git` suffix or the ssh form count as the same URL);
- every package name appears in exactly ONE repository entry per distro;
- every package tag is a live id in [`schema/tags.yaml`](schema/tags.yaml)
  (1–5 per package) — unknown and deprecated tags are rejected, with a
  did-you-mean suggestion;
- maintainers are real — `TBD` / `@example.com` placeholders are rejected.

So please register a `ref` that exists upstream, and list real maintainers
(name, email, GitHub handle).

## Validate locally

```bash
# 1. Schema + filename consistency + tag vocabulary + maintainer checks,
#    exactly as CI runs them — via pre-commit:
pipx install pre-commit        # or: pip install --user pre-commit
pre-commit run --all-files

# 2. Or run the individual checks directly:
pipx install check-jsonschema
check-jsonschema --schemafile schema/distribution.schema.json distributions/*.yaml
python scripts/check_distro_filename.py distributions/*.yaml
python scripts/check_tags.py distributions/*.yaml
python scripts/check_refs.py distributions/*.yaml          # needs network
```

The pre-commit `check-refs-offline` hook runs the placeholder-maintainer check
without network; CI runs the full `check_refs.py` including ref resolvability.

## Choosing a `ref` kind

| Kind     | When to use                                                     |
| -------- | --------------------------------------------------------------- |
| `tag`    | Freeze a known-good release. Validated once; immutable history. |
| `sha`    | Pin an exact commit. Same semantics as `tag`.                   |
| `branch` | "Always test my latest." Re-resolved and re-swept nightly.      |

See [`README.md`](README.md#ref-kinds) for details.

## Package tags

Every registered package carries 1–5 tags from the closed vocabulary in
[`schema/tags.yaml`](schema/tags.yaml). The vocabulary is the single source
of truth: each id has a one-line `summary` (rendered as the browse site's
tooltip) and, where a boundary is subtle, a `disambiguation` sentence.
`scripts/check_tags.py` rejects unknown and deprecated tags at PR time with a
did-you-mean suggestion.

### Choosing tags

- Put the package's **primary identity first** — the site renders tag chips
  in registry order.
- Tags are not mutually exclusive: a learned detector is
  `[perception, ml]`; a calibration RViz plugin is
  `[calibration, visualization]`.
- `tool` should rarely be a package's only tag — add the domain it serves
  (`[tool, map]`, not `[tool]`). CI emits a non-blocking warning otherwise.
- Read the `disambiguation` lines for the close pairs: `driver` vs
  `sensing`, `interface` vs `api`, `testing` vs `evaluation` vs `simulator`,
  `tool` vs `common-library`, and what counts as `launch`.

### Proposing a new tag

A PR editing `schema/tags.yaml` must include, in its description:

1. **Definition** — the one-line `summary` (plus a `disambiguation` if it
   borders an existing tag).
2. **Demand** — at least **two named, registerable ROS packages** (real
   `package.xml`, resolvable repo) that would carry it — ideally the same or
   a linked PR registers the first one. Vocabulary additions should trail
   demand, never lead it.
3. **Differentiation** — one sentence on why no existing tag _combination_
   covers those packages.
4. **Axis check** — the id is topical: not a maturity level (`stable`), not
   a hardware requirement (`gpu`), not a vendor name. Genuinely orthogonal
   axes become new typed schema fields, never tags.

Id style: lowercase, hyphen-separated (`common-library`), ≤ 20 characters,
singular, preferring the ecosystem's own noun (`simulator`, `launch`) and
established abbreviations (`ml`, `api`, `v2x`).

Deferred candidates and their adoption triggers (recorded so they are not
re-litigated):

| id              | adoption trigger                                                                                |
| --------------- | ----------------------------------------------------------------------------------------------- |
| `data`          | First registration of a `ros2bag_extensions`-class package; settle `data` vs `data-tools` then. |
| `teleoperation` | Two registerable teleoperation packages (e.g. the TUM stack lands).                             |
| `e2e`           | A second registerable end-to-end package (`ml` + `planning`/`control` cover it today).          |

Previously rejected (with reasons): `racing`, `hmi`, `fleet-management`,
`security`, `deployment` — no two registerable anchor packages exist;
`gpu`/`cuda` — a hardware requirement, wrong axis; `profiling` — folded into
`evaluation`'s definition.

### Renaming, merging, or retiring a tag

Tag ids are **never deleted or re-minted** once published — old provenance
headers and pinned registry refs must stay interpretable forever. One atomic
PR does all of:

1. add the new id under `tags:` (renames/merges; normal criteria apply);
2. move the old id under `deprecated:` with `replaced_by:` naming **live**
   tags (no chains) and a dated `note`;
3. retag **every** usage in `distributions/*.yaml`.

CI makes partial versions of this PR unmergeable: a deprecated tag in any
registry file is a hard error, and vocabulary edits re-run the check over
every distribution file. Tag edits never trigger re-validation sweeps — the
sweep diff tuple deliberately excludes tags — so a registry-wide migration
is a pure metadata change.

### When does `schema_version` bump?

Rule of thumb: **bump when readers must change to parse** (a shape change —
renamed fields, restructured values); **do not bump when the set of valid
documents merely narrows** within the same shape (adding the vocabulary,
`maxItems`, or a new semantic check). Narrowing keeps every deployed reader
(`aw-index-cli`, the site, the sweeps) working unchanged; bumping hard-fails
them all by design and demands a lockstep release.

## Repository conventions

- Conventional commit messages (`type(scope): description`).
- Keep changes minimal and scoped; one logical change per PR.
- Python tooling under `scripts/` targets Python 3.12 and depends only on
  `pyyaml` / `jsonschema` (CI installs them).
