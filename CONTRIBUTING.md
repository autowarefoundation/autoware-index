# Contributing to autoware-index

This repository is the **source of truth** for which community Autoware packages
exist and where their source lives. The primary contribution is **registering a
package** (or updating an existing registration). General Autoware contribution
guidelines apply too — see
<https://autowarefoundation.github.io/autoware-documentation/main/contributing/>.

## Registering or updating a package

1. **Fork** this repository.
2. Edit `distributions/<distro>.yaml` for each ROS distro you support — add or
   update an entry under `packages:`. The full field reference is in
   [`README.md`](README.md#schema-reference); the machine-checkable contract is
   [`schema/distribution.schema.json`](schema/distribution.schema.json).
3. **Validate locally** before opening a PR (see below).
4. Open a pull request. The `validate` workflow runs automatically. A maintainer
   reviews and merges.

After merge, the **eager sweep** validates your package's `ref` against the
current latest published Autoware release and appends a result to the
[`data`](../../tree/data) branch; `kind: branch` refs are additionally re-swept
nightly. The [browse site](README.md) renders the resulting compatibility table.

## What the validation enforces

A PR cannot merge unless, for every changed package:

- the file conforms to `schema/distribution.schema.json`;
- `ros_distro` equals the filename stem;
- the registered `ref` **actually resolves** in the named repository
  (`git ls-remote`) — a `tag`/`branch` that does not exist is rejected;
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
