# Contributing to autoware-index

This repository is the **source of truth** for which community Autoware packages
exist and where their source lives. The primary contribution is **registering
your repository's packages** (or updating an existing registration). General
Autoware contribution guidelines apply too — see
<https://autowarefoundation.github.io/autoware-documentation/main/contributing/>.

## Registering or updating packages

1. **Fork** this repository.
2. Edit `distributions/<distro>.yaml` for each ROS distro you support — add or
   update ONE entry under `repositories:` for your repository, listing every
   ROS package you register under its `packages:` map. The full field
   reference is in [`README.md`](README.md#schema-reference); the
   machine-checkable contract is
   [`schema/distribution.schema.json`](schema/distribution.schema.json).
3. **Validate locally** before opening a PR (see below).
4. Open a pull request. The `validate` workflow runs automatically. A maintainer
   reviews and merges.

After merge, the **eager sweep** clones your repository once at its registered
`ref`, validates every registered package against the current latest published
Autoware release, and appends one result per package to the
[`data`](../../tree/data) branch; `kind: branch` refs are additionally re-swept
nightly. The [browse site](README.md) renders the resulting per-package
compatibility tables.

## Registering a monorepo (several packages, one repository)

A repository hosting several ROS packages is **one** `repositories:` entry:

- The entry's key is the repository's name; each `packages:` key must equal
  the corresponding `package.xml` `<name>`. Don't register an entry named
  after the repository unless a package of that exact name exists — the sweep
  looks the package up with `colcon list` and fails loudly when it's absent.
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
  test failure never marks your package red (a broken in-repo *dependency*
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
- maintainers are real — `TBD` / `@example.com` placeholders are rejected.

So please register a `ref` that exists upstream, and list real maintainers
(name, email, GitHub handle).

## Validate locally

```bash
# 1. Schema + filename consistency + ref resolvability + maintainer checks,
#    exactly as CI runs them — via pre-commit:
pipx install pre-commit        # or: pip install --user pre-commit
pre-commit run --all-files

# 2. Or run the individual checks directly:
pipx install check-jsonschema
check-jsonschema --schemafile schema/distribution.schema.json distributions/*.yaml
python scripts/check_distro_filename.py distributions/*.yaml
python scripts/check_refs.py distributions/*.yaml          # needs network
```

The pre-commit `check-refs-offline` hook runs the placeholder-maintainer check
without network; CI runs the full `check_refs.py` including ref resolvability.

## Choosing a `ref` kind

| Kind | When to use |
|------|-------------|
| `tag` | Freeze a known-good release. Validated once; immutable history. |
| `sha` | Pin an exact commit. Same semantics as `tag`. |
| `branch` | "Always test my latest." Re-resolved and re-swept nightly. |

See [`README.md`](README.md#ref-kinds) for details.

## Repository conventions

- Conventional commit messages (`type(scope): description`).
- Keep changes minimal and scoped; one logical change per PR.
- Python tooling under `scripts/` targets Python 3.12 and depends only on
  `pyyaml` / `jsonschema` (CI installs them).
