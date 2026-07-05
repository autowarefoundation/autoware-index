# Autoware Index browse site

A static front-end rendered client-side from a generated `data.json`. No build
toolchain and no runtime dependencies beyond PyYAML.

```text
site/
  build.py      # data exporter: load registry + history -> write data.json, copy the assets
  index.html    # static shell (browse)
  styles.css    # styling (brand: white bg, primary #7290E5, secondary #EED45B)
  app.js        # fetch data.json -> render cards + compatibility tables, filters + repos builder
  compose.mjs   # aw-index-cli's registry->.repos composer, reused by the repos builder (see below)
  register.html # registration page shell (linked from the browse header)
  register.css  # registration page styles (shares the brand tokens from styles.css)
  register.js   # registration wizard: live YAML entry + client-side mirror of the PR gates
  sample-data/  # local-preview history + metadata fixtures (CI never reads it)
```

`compose.mjs` is the browser copy of aw-index-cli's canonical `js/compose.mjs`,
so the "repos builder" produces the exact same `.repos` file as the CLI. The
Pages deploy **fetches the latest aw-index-cli release** and bakes it in (see
Deployment), so it stays current without manual updates. The committed copy is
only a local/offline fallback.

`build.py` only loads the data and writes `data.json` next to copies of the
static assets (`STATIC_ASSETS`) in `--out`; everything you see is rendered in
the browser by `app.js` / `register.js`. To change the look or behaviour, edit
the static files directly; no Python is involved.

## What it shows

A filterable list of every registered package (search by name/tag/maintainer/
repository URL/repo name, filter by ROS distro, tag, and current status) with a
per-package **compatibility history** table built from sweep records: which
Autoware version each ref was tested against, pass/fail, the resolved commit,
and a link to the Actions run. Failing packages surface their last-green
Autoware version, and a package sharing its repository with registered siblings
wears a clickable "monorepo · N registered" badge on its card: clicking filters
the list to that repository's packages (clicking again clears), and hovering
or selecting any of them softly highlights the sibling cards that arrive in
the same clone.

## The registration page

`register.html` (linked from the browse header) turns "fork, hand-edit YAML,
hope CI passes" into a guided flow. Four stations (repository, ref, packages,
maintainers) write the `repositories:` entry live into a YAML preview, while a
"PR pre-flight" panel mirrors the same checks the `validate` workflow runs on
the pull request: schema shape (`check-jsonschema`), tag vocabulary
(`check_tags`), and uniqueness / placeholder-maintainer / ref-resolution rules
(`check_refs`). Registrations that already exist in `data.json` are rejected
client-side exactly like `check_refs.py` would (canonical-URL folding included).

For github.com repositories the page also auto-discovers through the public
GitHub API (no token, 60 requests/hour): repository metadata, branches and tags
for the ref picker, and a tree scan that finds and parses `package.xml` files to
prefill package names, descriptions, and maintainer suggestions (GitHub handles
are resolved from the maintainer emails via the repo's commit history,
noreply-address parsing, or public-profile search, where possible). Everything
degrades to manual entry: the API is a convenience; CI remains the authority.

There is no backend, so the handoff goes through GitHub: the primary action
opens the `register-request.yml` issue form pre-filled with the entry;
submitting it makes the `register` workflow apply the entry programmatically
(`scripts/apply_registration.py`), run the offline gates, and open a pull
request authored and signed off as the requester. The manual fallback copies
the whole updated registry file (the current `distributions/<distro>.yaml`
fetched from `main` with the entry appended: replacing the full file survives
web editors that re-indent pasted YAML fragments; copying just the entry
remains available) and deep-links to editing the file on GitHub, with a
conventional-commit PR title suggested.

## The two-branch join

The site joins data from two branches:

| Source                                                            | Branch          | Read by               |
| ----------------------------------------------------------------- | --------------- | --------------------- |
| `distributions/<distro>.yaml` (what is registered)                | `main`          | `--distributions-dir` |
| `history/<distro>/<package>.ndjson` (how it validated)            | `data` (orphan) | `--history-dir`       |
| `metadata/<distro>/<package>.xml` (cached upstream `package.xml`) | `data` (orphan) | `--metadata-dir`      |

Each card's description is the registry-side `description:` override if set,
otherwise the cached `package.xml` `<description>`.

CI checks out both and runs the generator. There is **no real history until the
first sweep runs**, so before then the compatibility tables read "not yet swept".

## Preview locally

With the sample fixture (so the table is populated even before a real sweep):

```bash
python site/build.py \
  --distributions-dir distributions \
  --history-dir site/sample-data/history \
  --metadata-dir site/sample-data/metadata \
  --out _site
python -m http.server -d _site    # then open http://localhost:8000
```

You **must** serve over HTTP: opening `_site/index.html` via `file://` won't
work, because `app.js` `fetch()`es `data.json` and browsers block that on the
`file://` scheme.

Against real data, check out the `data` branch somewhere and point
`--history-dir` at its `history/` directory. The `sample-data/` fixture is for
local preview only; CI never reads it.

## Deployment

`.github/workflows/pages.yaml` builds and deploys to GitHub Pages. It checks out
`main` and the `data` branch (into `_data/`), fetches the latest aw-index-cli
release's `js/compose.mjs` into `site/compose.mjs` (falling back to the committed
copy if unreachable), runs `build.py`, and publishes `_site/`. It triggers on pushes to `main` (registry/site changes),
`workflow_dispatch`, and a periodic schedule that picks up new `data`-branch
sweep records (the `data` branch can't trigger this workflow itself: it's an
orphan branch with no workflow files). GitHub Pages must be enabled with
"GitHub Actions" as the source.
