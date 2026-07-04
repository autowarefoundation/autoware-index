# autoware-index

The **Autoware Index** is a registry of community ROS 2 packages that extend
[Autoware](https://github.com/autowarefoundation/autoware). This repository records which packages
exist, where their source lives, and whether each one builds and passes tests against the
latest Autoware release.

- **Browse packages:** <https://autowarefoundation.github.io/autoware-index/>
- **Register your packages:** <https://autowarefoundation.github.io/autoware-index/register.html>

The Index complements Autoware's Core layer (see
[autowarefoundation/autoware#7090](https://github.com/autowarefoundation/autoware/issues/7090)).
Community packages register themselves here; the browse site,
[`aw-index-cli`](https://github.com/autowarefoundation/aw-index-cli), and CI consume the registry.

## How it works

1. A maintainer registers a **repository** in `distributions/<distro>.yaml`: one git URL, one
   `ref`, and the ROS packages it hosts. A monorepo is a single entry registering several
   packages, all validated in lockstep at that one ref.
2. After merge, CI clones the repository at its ref and builds + tests every registered package
   against the **current latest Autoware release**. `branch` refs are re-validated nightly;
   `tag`/`sha` refs are pinned and validated once per change. New Autoware releases are picked
   up automatically on the next sweep.
3. Each run appends one record per package to the orphan
   [`data`](https://github.com/autowarefoundation/autoware-index/tree/data) branch
   (`history/<distro>/<package>.ndjson`, append-only): the tested ref, resolved commit, Autoware
   version, pass/fail status, and a link to the Actions run.
4. The [browse site](https://autowarefoundation.github.io/autoware-index/) joins the registry
   with that history into searchable package cards with per-package compatibility tables.

## Using packages

1. Pick your packages on the [browse site](https://autowarefoundation.github.io/autoware-index/).
2. Open its **repos builder** and download your selection as a
   [vcs2l](https://github.com/ros-infrastructure/vcs2l) `.repos` file.
3. Import the file into your workspace: `vcs import src < autoware-index.repos`.

Or do the same from the command line with
[`aw-index-cli`](https://github.com/autowarefoundation/aw-index-cli) (`pipx install aw-index-cli`).

## Registering your packages

Use the [guided register page](https://autowarefoundation.github.io/autoware-index/register.html):
it writes the registry entry for you (auto-discovering packages from GitHub-hosted repositories),
runs the same checks as the PR gate client-side, and submits a registration request that opens
the pull request on your behalf.

Or by hand: fork this repo, add ONE entry under `repositories:` in
`distributions/<distro>.yaml` for each distro you support, and open a pull request. The
`validate` workflow checks schema conformance, ref resolvability, and uniqueness; the
`build-check` workflow builds + tests added or changed entries against the current Autoware
release as an advisory signal for the reviewer. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
full walkthrough and local validation.

## Registry format

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
      <package_name>: # key MUST equal the package.xml <name>
        tags: [sensing, perception, planning, ...]
        description: ... # optional; overrides the cached package.xml text
        maintainers: [...] # optional per-package override
```

Key rules:

- **One entry per repository, exactly one `ref`.** All of a repository's packages validate and
  distribute at that single ref. A package that needs a different ref moves to its own
  repository entry. Repository URLs and package names are unique per distro file.
- **`packages:` keys are real ROS package names**: each must equal the `package.xml` `<name>`
  at the registered ref, never the repository name (unless they genuinely coincide).
- **`tags`** are 1–5 entries from the closed vocabulary in [`schema/tags.yaml`](schema/tags.yaml).
- **`description`** is optional; omit it to show the upstream `package.xml` `<description>`.

| `ref` kind  | Sweep behaviour                                                                                    |
| ----------- | -------------------------------------------------------------------------------------------------- |
| `tag`/`sha` | Pinned. Validated once against the current Autoware release; re-validated only if the ref changes. |
| `branch`    | Rolling. Re-resolved and re-validated nightly at the branch tip.                                   |

Use a `branch` ref for "always test my latest", `tag` or `sha` to freeze a known-good release.
The full machine-checkable contract is
[`schema/distribution.schema.json`](schema/distribution.schema.json).

## Repository layout

```text
distributions/        # the registry: one YAML file per ROS distro
schema/               # JSON Schemas (registry + history records) and the tag vocabulary
scripts/              # validation + sweep pipeline (Python 3.12)
site/                 # browse-site generator and static assets
.github/workflows/    # PR gates, sweeps, site deploy, registration bot
```

## Development

```bash
pipx install pre-commit
pre-commit run --all-files # mirrors the PR checks
python -m pytest           # script tests
```

To preview the browse site locally, see [`site/README.md`](site/README.md).

## Related repositories

- [`aw-index-cli`](https://github.com/autowarefoundation/aw-index-cli): the consumer CLI. It
  composes a `.repos` file from the registry (`compose`), checks a workspace against it
  (`check`), and lists packages with their latest validation status (`list`).
- [`autoware-index-github-actions`](https://github.com/autowarefoundation/autoware-index-github-actions)
  hosts the reusable workflows that do the actual per-repository build + test, plus
  the `latest-autoware-version` resolver.
