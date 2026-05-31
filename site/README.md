# Autoware Index browse site

A static front-end rendered client-side from a generated `data.json`. No build
toolchain and no runtime dependencies beyond PyYAML.

```
site/
  build.py      # data exporter: load registry + history -> write data.json, copy the assets
  index.html    # static shell
  styles.css    # styling (brand: white bg, primary #7290E5, secondary #EED45B)
  app.js        # fetch data.json -> render cards + compatibility tables, wire filters
  sample-data/  # local-preview history + metadata fixtures (CI never reads it)
```

`build.py` only loads the data and writes `data.json` next to copies of
`index.html` / `styles.css` / `app.js` in `--out`; everything you see is rendered
in the browser by `app.js`. To change the look or behaviour, edit those three
static files directly — no Python involved.

## What it shows

A filterable list of every registered package (search by name/tag/maintainer,
filter by ROS distro, tag, and current status) with a per-package
**compatibility history** table built from sweep records: which Autoware version
each ref was tested against, pass/fail, the resolved commit, and a link to the
Actions run. Failing packages surface their last-green Autoware version.

## The two-branch join

The site joins data from two branches:

| Source | Branch | Read by |
|--------|--------|---------|
| `distributions/<distro>.yaml` (what is registered) | `main` | `--distributions-dir` |
| `history/<distro>/<package>.ndjson` (how it validated) | `data` (orphan) | `--history-dir` |
| `metadata/<distro>/<package>.xml` (cached upstream `package.xml`) | `data` (orphan) | `--metadata-dir` |

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

You **must** serve over HTTP — opening `_site/index.html` via `file://` won't
work, because `app.js` `fetch()`es `data.json` and browsers block that on the
`file://` scheme.

Against real data, check out the `data` branch somewhere and point
`--history-dir` at its `history/` directory. The `sample-data/` fixture is for
local preview only — CI never reads it.

## Deployment

`.github/workflows/pages.yaml` builds and deploys to GitHub Pages. It checks out
`main` and the `data` branch (into `_data/`), runs `build.py`, and publishes
`_site/`. It triggers on pushes to `main` (registry/site changes),
`workflow_dispatch`, and a periodic schedule that picks up new `data`-branch
sweep records (the `data` branch can't trigger this workflow itself — it's an
orphan branch with no workflow files). GitHub Pages must be enabled with
"GitHub Actions" as the source.
