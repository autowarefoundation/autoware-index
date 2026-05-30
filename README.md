# autoware-index

Federated registry of community Autoware packages. This repo is the **source of truth** for which packages exist and where their source lives. Validation history (each package × the Autoware version it was tested against) lives on the orphan `data` branch.

The Index layer complements the Core and Universe layers of the Autoware architecture (see [autowarefoundation/autoware#7090](https://github.com/autowarefoundation/autoware/issues/7090)). Community packages register themselves here; downstream tooling (the `aw-index-cli`, the browse site, CI workflows) consumes the registry.

## Repository layout

```
distributions/                          # one file per supported ROS distro
  jazzy.yaml
schema/
  distribution.schema.json              # JSON Schema for distributions/*.yaml
  history-record.schema.json            # JSON Schema for one validation history NDJSON line
.github/workflows/
  validate.yaml                         # PR-time schema + ros_distro/filename consistency check
```

The orphan [`data`](https://github.com/autowarefoundation/autoware-index/tree/data) branch carries validation history at `history/<distro>/<package>.ndjson`. See **Validation history** below.

## How to register a package

1. Fork this repo.
2. Edit `distributions/<distro>.yaml` for each ROS distro you support. Add an entry under `packages:`.
3. Open a pull request. The `validate` workflow runs schema + filename consistency checks automatically.
4. A maintainer reviews and merges.

After merge, the **eager sweep** workflow validates your package's `ref` against the current latest published Autoware release (resolved by [`latest-autoware-version`](https://github.com/autowarefoundation/autoware-index-github-actions/blob/main/.github/actions/latest-autoware-version/action.yaml) in the actions repo) and appends a history record to the `data` branch.

## Schema reference

Each `distributions/<distro>.yaml` looks like:

```yaml
schema_version: "1"
ros_distro: jazzy                       # MUST equal the filename stem

packages:
  <package_name>:
    repository: https://github.com/...
    governance: community | foundation
    tags: [sensing, perception, planning, ...]
    maintainers:
      - { name: ..., email: ..., github: ... }
    ref:
      kind: tag | sha | branch
      value: "<tag-name | full-sha | branch-name>"
```

| Field | Meaning |
|-------|---------|
| `schema_version` | Currently `"1"`. Bumped only on breaking changes. |
| `ros_distro` | Lowercase ROS 2 distro name. Validate workflow asserts it matches the filename stem. |
| `packages.<name>.repository` | Git remote URL — what `vcs import` will clone. |
| `packages.<name>.governance` | `community` for outside-foundation packages; `foundation` for those owned by the Autoware Foundation. |
| `packages.<name>.tags` | Free-form taxonomy (`sensing`, `perception`, `planning`, ...). |
| `packages.<name>.maintainers` | Contact list. Sweeps will surface failures here in the future. |
| `packages.<name>.ref` | Single ref per (package, distro). See next section. |

Sweeps always validate against the current latest published Autoware release; the index no longer tracks Autoware versions. The Autoware version a record was tested against is captured in each history line.

Full schema: [`schema/distribution.schema.json`](schema/distribution.schema.json).

## `ref` kinds

Exactly one `ref` per (package, distro). Switching kinds is a PR that overwrites the entry; history remembers the old ref.

| Kind | Resolution | Sweep behaviour |
|------|------------|-----------------|
| `tag` | The git tag named in `value`. | Pinned. Validated once against the current latest Autoware release; the result is an immutable history row. |
| `sha` | The exact 40-char commit sha. | Pinned. Same semantics as `tag`. |
| `branch` | The branch named in `value`, re-resolved at every sweep via `git ls-remote`. | Rolling. Validated by the nightly sweep against the current latest Autoware release; each run appends a new history row. |

Maintainers who want "always test my latest" should use a `branch` ref. Those who want "freeze a known-good release" should use `tag` or `sha`.

## Validation history

Sweep workflows (Phase 4 — not yet built) commit append-only NDJSON to the orphan `data` branch:

```
data:history/<distro>/<package>.ndjson
```

Each line is one record conforming to [`schema/history-record.schema.json`](schema/history-record.schema.json), capturing the swept `(ref_at_test, resolved_sha, autoware_version, status, at, actions_run_url)`. No retention pruning. No failure side effects — failing sweeps record `status: fail` and move on. Consumers and the future browse site render the per-package compatibility table from these records.

Two sweep modes (see `.claude/plan.md` for the design):
- **Eager** — fires when a package's `ref` field changes via PR merge. Tests that one package against the current latest Autoware release.
- **Nightly** — daily UTC cron. Re-resolves every `kind: branch` entry and tests it against the current latest Autoware release.

The Autoware version each sweep targets is resolved at runtime by [`latest-autoware-version`](https://github.com/autowarefoundation/autoware-index-github-actions/blob/main/.github/actions/latest-autoware-version/action.yaml). New Autoware releases are picked up on the next sweep automatically; no PR or poller is required to wire them in.

## Local development workspace

The repo reserves `ros2_ws/` at the root as a **gitignored scratch ROS 2 workspace** for experimentation:

```
ros2_ws/
  src/          # vcs import packages here
  build/  install/  log/   # colcon outputs
```

Use it for ad-hoc verification (compose a `.repos` slice from this registry, `vcs import` into `ros2_ws/src/`, `colcon build`). CI workflows that need a real ROS workspace will also operate inside this path. Nothing under `ros2_ws/` is ever tracked.

## Local validation

```bash
pipx install check-jsonschema      # or: pip install --user check-jsonschema
check-jsonschema \
  --schemafile schema/distribution.schema.json \
  distributions/*.yaml
```

The validate workflow runs the same command on every PR, plus a filename/`ros_distro` consistency check.

## Related repositories

- `aw-index-cli` (TBD) — `compose` / `import` / `sync` / `check` / `refresh` commands against this registry.
- `autoware-index-github-actions` (TBD) — reusable validation workflow consumed by the registry's sweep workflows.
- `autoware-documentation` — federation guide and registration walkthrough land under `docs/contribute/autoware-index/`.
