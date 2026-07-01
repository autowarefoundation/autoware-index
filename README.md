# autoware-index

Federated registry of community Autoware packages. This repo is the **source of truth** for which packages exist and where their source lives. Registration is keyed by **repository**: each entry owns one git URL and ONE ref, and lists the registered ROS packages it hosts — a monorepo registers several packages in one entry, all validated in lockstep at that single ref. Validation history (each package × the Autoware version it was tested against) lives on the orphan `data` branch, keyed by package.

The Index layer complements the Core and Universe layers of the Autoware architecture (see [autowarefoundation/autoware#7090](https://github.com/autowarefoundation/autoware/issues/7090)). Community packages register themselves here; downstream tooling (the `aw-index-cli`, the browse site, CI workflows) consumes the registry.

## Repository layout

```
distributions/                          # one file per supported ROS distro
  jazzy.yaml
schema/
  distribution.schema.json              # JSON Schema for distributions/*.yaml
  history-record.schema.json            # JSON Schema for one validation history NDJSON line
scripts/                                # validation + sweep + site helpers (Python 3.12)
site/                                   # browse-site generator (site/build.py)
.github/workflows/
  validate.yaml                         # PR-time schema + filename + ref/uniqueness checks
  sweep-eager.yaml                      # level-triggered sweep of changed repository entries
  sweep-nightly.yaml                    # daily re-validation of kind:branch repositories
  pages.yaml                            # build + deploy the browse site to GitHub Pages
```

The orphan [`data`](https://github.com/autowarefoundation/autoware-index/tree/data) branch carries validation history at `history/<distro>/<package>.ndjson`, the per-repository sweep cursors at `state/<distro>/<repo_name>.json`, and the cached upstream `package.xml` files at `metadata/<distro>/<package>.xml`. See **Validation history** below.

## How to register your packages

1. Fork this repo.
2. Edit `distributions/<distro>.yaml` for each ROS distro you support. Add ONE entry under `repositories:` for your repository, and list every ROS package you are registering under its `packages:` map — each key must equal the package's `package.xml` `<name>`, **never** the repository name (unless they genuinely coincide).
3. Open a pull request. The `validate` workflow automatically checks schema conformance, `ros_distro`/filename consistency, that your `ref` resolves upstream, that no other entry already registers your repository URL or package names, and that maintainers are real (no placeholders).
4. A maintainer reviews and merges.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full walkthrough and local validation.

After merge, the **eager sweep** workflow validates your repository's `ref` against the current latest published Autoware release (resolved by [`latest-autoware-version`](https://github.com/autowarefoundation/autoware-index-github-actions/blob/main/.github/actions/latest-autoware-version/action.yaml) in the actions repo) and appends one history record per registered package to the `data` branch; `kind: branch` refs are additionally re-validated by the **nightly sweep**.

## Schema reference

Each `distributions/<distro>.yaml` looks like:

```yaml
schema_version: "2"
ros_distro: jazzy # MUST equal the filename stem

repositories:
  <repo_name>: # registry-unique key for the repository
    url: https://github.com/...
    ref: # ONE ref per repository (lockstep)
      kind: tag | sha | branch
      value: "<tag-name | full-sha | branch-name>"
    governance: community | foundation
    maintainers: # repo-level default
      - { name: ..., email: ..., github: ... }
    packages: # every registered package this repo hosts
      <package_name>: # key == package.xml <name>
        tags: [sensing, perception, planning, ...]
        description: ... # optional; overrides cached package.xml
        maintainers: [...] # optional per-package override
```

| Field                             | Meaning                                                                                                                                                                                                                      |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`                  | Currently `"2"`. Every reader (sweeps, checks, site, CLI) gates on it and hard-fails on an unsupported version.                                                                                                              |
| `ros_distro`                      | Lowercase ROS 2 distro name. Validate workflow asserts it matches the filename stem.                                                                                                                                         |
| `repositories.<repo>.url`         | Git remote URL — what `vcs import` will clone (the WHOLE repository, including any unregistered sibling packages the index makes no claims about). One entry per repository; spelling variants of the same URL are rejected. |
| `repositories.<repo>.ref`         | Single ref per (repository, distro). Every registered package of the repository is validated and distributed at this one ref. See next section.                                                                              |
| `repositories.<repo>.governance`  | `community` for outside-foundation repositories; `foundation` for those owned by the Autoware Foundation.                                                                                                                    |
| `repositories.<repo>.maintainers` | Default contact list for the repository's packages.                                                                                                                                                                          |
| `...packages.<name>`              | One entry per registered ROS package; the key must equal the package's `package.xml` `<name>`. Package names are unique across the whole distro file.                                                                        |
| `...packages.<name>.tags`         | Free-form taxonomy (`sensing`, `perception`, `planning`, ...).                                                                                                                                                               |
| `...packages.<name>.description`  | _(optional)_ one-line card summary. Omit it to show the cached `package.xml` `<description>`; set it to override that upstream text.                                                                                         |
| `...packages.<name>.maintainers`  | _(optional)_ per-package override of the repository default.                                                                                                                                                                 |

Sweeps always validate against the current latest published Autoware release; the index does not track Autoware versions. The Autoware version a record was tested against is captured in each history line.

Full schema: [`schema/distribution.schema.json`](schema/distribution.schema.json).

## `ref` kinds

Exactly one `ref` per (repository, distro) — there are no per-package refs: one repository @ one sha is one source state, and `vcs import` can only materialize one checkout per repository. A package that needs a different ref than its siblings must move to its own repository. Switching refs is a PR that overwrites the entry; history remembers the old ref.

| Kind     | Resolution                                               | Sweep behaviour                                                                                                                                 |
| -------- | -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `tag`    | The git tag named in `value`.                            | Pinned. Validated once against the current latest Autoware release; the result is an immutable history row per registered package.              |
| `sha`    | The exact 40-char commit sha.                            | Pinned. Same semantics as `tag`.                                                                                                                |
| `branch` | The branch named in `value`, re-resolved at every sweep. | Rolling. Validated by the nightly sweep against the current latest Autoware release; each run appends a new history row per registered package. |

Maintainers who want "always test my latest" should use a `branch` ref. Those who want "freeze a known-good release" should use `tag` or `sha`.

## Validation history

Sweep workflows commit append-only NDJSON to the orphan `data` branch:

```
data:history/<distro>/<package>.ndjson
```

Each line is one record conforming to [`schema/history-record.schema.json`](schema/history-record.schema.json), capturing the swept `(ref_at_test, resolved_sha, autoware_version, status, at, actions_run_url, repository, repo_name)`. History stays **per-package** even though sweeps run per repository: a repository row fans out into one record per registered package, each with an independently derived, honest status — a package's `pass` means _its own_ dependency-closure build and _its own_ tests succeeded at that repo@sha; a sibling package's tests never touch it. Sibling records of one sweep share `repository`, `resolved_sha`, and `actions_run_url`, so same-event grouping is exact. No retention pruning. No failure side effects — failing sweeps record `status: fail` and move on. Consumers and the browse site render the per-package compatibility table from these records.

The `data` branch also carries two **mutable caches** (only `history/` is append-only): `state/<distro>/<repo_name>.json` — the per-repository cursor of what was last conclusively recorded, written in the same commit as the records — and `metadata/<distro>/<package>.xml` — each package's pristine upstream `package.xml` for the site's card descriptions.

Two sweep modes:

- **Eager** — level-triggered on every push to `main` touching `distributions/` (plus manual dispatch): every repository entry whose `(url, ref, registered package set)` differs from its `state/` cursor is swept. Lost or cancelled runs self-heal — the delta is simply re-detected next time.
- **Nightly** — daily UTC cron. Re-resolves every `kind: branch` repository at its branch tip, plus the same state-diff as catch-up.

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

The quickest way is [`pre-commit`](.pre-commit-config.yaml), which mirrors CI:

```bash
pipx install pre-commit        # or: pip install --user pre-commit
pre-commit run --all-files
```

Or run the checks individually:

```bash
pipx install check-jsonschema      # or: pip install --user check-jsonschema
check-jsonschema --schemafile schema/distribution.schema.json distributions/*.yaml
python scripts/check_distro_filename.py distributions/*.yaml
python scripts/check_refs.py distributions/*.yaml          # ref resolvability + real maintainers (needs network)
```

The validate workflow runs the same checks on every PR.

## Browse site

`site/build.py` renders a filterable browse view with a per-package compatibility
table, joining the registrations on `main` with the validation history on `data`.
It deploys to GitHub Pages via `.github/workflows/pages.yaml`. To preview locally,
see [`site/README.md`](site/README.md).

## Related repositories

- [`autoware-index-github-actions`](https://github.com/autowarefoundation/autoware-index-github-actions) — reusable sweep/validate workflows + the `latest-autoware-version` resolver consumed by the sweep workflows.
- `aw-index-cli` (planned) — `compose` / `import` / `sync` / `check` / `refresh` commands against this registry.
- `autoware-documentation` (planned) — federation guide and registration walkthrough under `docs/contributing/`.
