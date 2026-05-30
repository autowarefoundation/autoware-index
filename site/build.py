#!/usr/bin/env python3
"""Build the Autoware Index browse site from the registry + validation history.

The site joins two branches:
  - main:  distributions/<distro>.yaml   (what is registered)
  - data:  history/<distro>/<package>.ndjson   (how it has validated)

In CI the `data` branch is checked out into a sibling path and passed via
--history-dir; locally you can point --history-dir at site/sample-data/history
to preview a populated table before any real sweep has run.

Output is a single self-contained index.html (inline CSS + vanilla JS for
client-side filtering — no build toolchain, no runtime dependencies beyond
PyYAML). Open it directly or serve the output dir with `python -m http.server`.

Usage:
    site/build.py --distributions-dir distributions \\
                  --history-dir _data/history \\
                  --out _site
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import yaml

STATUS_LABELS = {"pass": "passing", "fail": "failing", "unknown": "not yet swept"}


def semver_key(version: str) -> tuple:
    """Sort key for an X.Y.Z SemVer string; non-numeric parts sort last."""
    parts = []
    for piece in str(version).split("."):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def load_distributions(distributions_dir: Path) -> list[dict]:
    """Flatten distributions/*.yaml into one registration record per package."""
    registrations: list[dict] = []
    for path in sorted(distributions_dir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        distro = doc.get("ros_distro") or path.stem
        for name, spec in (doc.get("packages") or {}).items():
            spec = spec or {}
            registrations.append(
                {
                    "distro": distro,
                    "name": name,
                    "repository": spec.get("repository", ""),
                    "governance": spec.get("governance", "community"),
                    "tags": spec.get("tags") or [],
                    "maintainers": spec.get("maintainers") or [],
                    "ref": spec.get("ref") or {},
                }
            )
    return registrations


def load_history(history_dir: Path) -> dict[tuple[str, str], list[dict]]:
    """Read history/<distro>/<package>.ndjson into {(distro, package): [records]}."""
    history: dict[tuple[str, str], list[dict]] = {}
    if not history_dir or not history_dir.is_dir():
        return history
    for ndjson in sorted(history_dir.glob("*/*.ndjson")):
        distro = ndjson.parent.name
        package = ndjson.stem
        records = []
        for line in ndjson.read_text().splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        history[(distro, package)] = records
    return history


def summarize(records: list[dict]) -> dict:
    """Derive current status, last-green version, and a per-version latest cell."""
    if not records:
        return {"current_status": "unknown", "last_green": None, "last_tested_at": None, "versions": []}

    by_time = sorted(records, key=lambda r: r.get("at", ""))
    latest_overall = by_time[-1]

    # Latest record per autoware_version (most recent `at` wins).
    latest_per_version: dict[str, dict] = {}
    for rec in by_time:
        latest_per_version[rec.get("autoware_version", "?")] = rec

    greens = [r for r in by_time if r.get("status") == "pass"]
    last_green = greens[-1].get("autoware_version") if greens else None

    versions = [
        {
            "autoware_version": ver,
            "status": rec.get("status", "unknown"),
            "ref_at_test": rec.get("ref_at_test", {}),
            "resolved_sha": rec.get("resolved_sha", ""),
            "at": rec.get("at", ""),
            "actions_run_url": rec.get("actions_run_url", ""),
        }
        for ver, rec in sorted(latest_per_version.items(), key=lambda kv: semver_key(kv[0]), reverse=True)
    ]

    return {
        "current_status": latest_overall.get("status", "unknown"),
        "last_green": last_green,
        "last_tested_at": latest_overall.get("at"),
        "versions": versions,
    }


def build_packages(registrations: list[dict], history: dict[tuple[str, str], list[dict]]) -> list[dict]:
    packages = []
    for reg in registrations:
        records = history.get((reg["distro"], reg["name"]), [])
        packages.append({**reg, **summarize(records)})
    packages.sort(key=lambda p: (p["name"], p["distro"]))
    return packages


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def status_pill(status: str) -> str:
    return f'<span class="pill pill-{esc(status)}">{esc(STATUS_LABELS.get(status, status))}</span>'


def render_version_table(versions: list[dict]) -> str:
    if not versions:
        return '<p class="muted">No validation runs recorded yet.</p>'
    rows = []
    for v in versions:
        ref = v["ref_at_test"]
        ref_str = f'{esc(ref.get("kind", ""))} {esc(ref.get("value", ""))}'.strip()
        sha = esc(v["resolved_sha"][:12]) if v["resolved_sha"] else "—"
        when = esc(v["at"][:10]) if v["at"] else "—"
        run = v["actions_run_url"]
        sha_cell = f'<a href="{esc(run)}" title="View run">{sha}</a>' if run else sha
        rows.append(
            f"<tr><td>autoware {esc(v['autoware_version'])}</td>"
            f"<td>{status_pill(v['status'])}</td>"
            f"<td><code>{ref_str}</code></td>"
            f"<td><code>{sha_cell}</code></td>"
            f"<td>{when}</td></tr>"
        )
    return (
        '<table class="versions"><thead><tr>'
        "<th>Autoware</th><th>Status</th><th>Ref tested</th><th>Commit</th><th>Tested</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_maintainers(maintainers: list[dict]) -> str:
    if not maintainers:
        return ""
    links = []
    for m in maintainers:
        gh = m.get("github", "")
        name = esc(m.get("name", gh))
        links.append(f'<a href="https://github.com/{esc(gh)}">{name}</a>' if gh else name)
    return '<div class="maintainers">Maintainers: ' + ", ".join(links) + "</div>"


def render_card(pkg: dict) -> str:
    tags = pkg["tags"]
    tag_html = "".join(f'<span class="tag">{esc(t)}</span>' for t in tags)
    status = pkg["current_status"]
    note = ""
    if status == "fail" and pkg["last_green"]:
        note = f'<span class="muted"> · last green: autoware {esc(pkg["last_green"])}</span>'
    ref = pkg["ref"]
    ref_str = f'{esc(ref.get("kind", ""))} {esc(ref.get("value", ""))}'.strip()
    data_attrs = (
        f'data-name="{esc(pkg["name"].lower())}" '
        f'data-distro="{esc(pkg["distro"])}" '
        f'data-status="{esc(status)}" '
        f'data-tags="{esc(" ".join(tags))}" '
        f'data-search="{esc((pkg["name"] + " " + " ".join(tags) + " " + " ".join(m.get("name", "") for m in pkg["maintainers"])).lower())}"'
    )
    return f"""<article class="card" {data_attrs}>
  <header>
    <h2>{esc(pkg["name"])}</h2>
    <div class="badges">
      <span class="badge badge-distro">{esc(pkg["distro"])}</span>
      <span class="badge badge-gov">{esc(pkg["governance"])}</span>
      {status_pill(status)}{note}
    </div>
  </header>
  <div class="tags">{tag_html}</div>
  <div class="meta">
    <a class="repo" href="{esc(pkg["repository"])}">{esc(pkg["repository"])}</a>
    <span class="muted"> · registered ref: <code>{ref_str}</code></span>
  </div>
  {render_maintainers(pkg["maintainers"])}
  <details><summary>Compatibility history</summary>{render_version_table(pkg["versions"])}</details>
</article>"""


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Autoware Index</title>
<style>
  :root {{ --bg:#0d1117; --panel:#161b22; --border:#30363d; --fg:#e6edf3; --muted:#8b949e;
           --accent:#2dd4bf; --pass:#2ea043; --fail:#da3633; --unknown:#6e7681; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
          font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  header.site {{ padding:32px 24px 16px; border-bottom:1px solid var(--border); }}
  header.site h1 {{ margin:0; font-size:26px; }}
  header.site h1 span {{ color:var(--accent); }}
  header.site p {{ margin:6px 0 0; color:var(--muted); max-width:64ch; }}
  .stats {{ display:flex; gap:24px; margin-top:14px; color:var(--muted); font-size:13px; }}
  .stats b {{ color:var(--fg); }}
  .wrap {{ max-width:980px; margin:0 auto; padding:20px 24px 64px; }}
  .controls {{ display:flex; flex-wrap:wrap; gap:10px; margin:18px 0 22px; position:sticky; top:0;
               background:var(--bg); padding:12px 0; z-index:5; }}
  .controls input, .controls select {{ background:var(--panel); color:var(--fg);
               border:1px solid var(--border); border-radius:7px; padding:8px 10px; font-size:14px; }}
  .controls input[type=search] {{ flex:1; min-width:200px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:12px;
           padding:18px 20px; margin-bottom:14px; }}
  .card header {{ display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap; }}
  .card h2 {{ margin:0; font-size:18px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .badges {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  .badge {{ font-size:12px; padding:2px 8px; border-radius:20px; border:1px solid var(--border); color:var(--muted); }}
  .badge-distro {{ border-color:var(--accent); color:var(--accent); }}
  .pill {{ font-size:12px; font-weight:600; padding:2px 9px; border-radius:20px; color:#fff; }}
  .pill-pass {{ background:var(--pass); }}
  .pill-fail {{ background:var(--fail); }}
  .pill-unknown {{ background:var(--unknown); }}
  .tags {{ margin:10px 0 6px; display:flex; gap:6px; flex-wrap:wrap; }}
  .tag {{ font-size:12px; background:#21262d; border:1px solid var(--border); border-radius:6px; padding:1px 8px; color:var(--muted); }}
  .meta {{ font-size:13px; }}
  .repo {{ color:var(--accent); text-decoration:none; word-break:break-all; }}
  .maintainers {{ font-size:13px; color:var(--muted); margin-top:6px; }}
  .maintainers a, .versions a {{ color:var(--accent); text-decoration:none; }}
  .muted {{ color:var(--muted); }}
  code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; }}
  details {{ margin-top:12px; }}
  summary {{ cursor:pointer; color:var(--accent); font-size:13px; }}
  table.versions {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:13px; }}
  table.versions th, table.versions td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); }}
  table.versions th {{ color:var(--muted); font-weight:500; }}
  .empty {{ color:var(--muted); padding:40px 0; text-align:center; }}
  footer {{ color:var(--muted); font-size:12px; text-align:center; padding:24px; border-top:1px solid var(--border); }}
  footer a {{ color:var(--accent); }}
</style>
</head>
<body>
<header class="site">
  <h1>Autoware <span>Index</span></h1>
  <p>Federated registry of community Autoware packages. Each package is validated against the
     latest published Autoware release; the compatibility history below is built from sweep records
     on the <a href="https://github.com/autowarefoundation/autoware-index/tree/data" style="color:var(--accent)">data branch</a>.</p>
  <div class="stats">
    <span><b>{n_packages}</b> packages</span>
    <span><b>{n_distros}</b> distributions</span>
    <span>built <b>{built_at}</b></span>
  </div>
</header>
<div class="wrap">
  <div class="controls">
    <input type="search" id="q" placeholder="Search packages, tags, maintainers…" autocomplete="off">
    <select id="distro"><option value="">All distros</option>{distro_options}</select>
    <select id="tag"><option value="">All tags</option>{tag_options}</select>
    <select id="status">
      <option value="">Any status</option>
      <option value="pass">Passing</option>
      <option value="fail">Failing</option>
      <option value="unknown">Not yet swept</option>
    </select>
  </div>
  <div id="cards">{cards}</div>
  <div id="empty" class="empty" hidden>No packages match your filters.</div>
</div>
<footer>
  Generated by <code>site/build.py</code> · source of truth:
  <a href="https://github.com/autowarefoundation/autoware-index">autowarefoundation/autoware-index</a>
</footer>
<script>
  const q = document.getElementById('q'), distro = document.getElementById('distro'),
        tag = document.getElementById('tag'), status = document.getElementById('status'),
        cards = [...document.querySelectorAll('.card')], empty = document.getElementById('empty');
  function apply() {{
    const t = q.value.trim().toLowerCase(), d = distro.value, g = tag.value, s = status.value;
    let visible = 0;
    for (const c of cards) {{
      const ok = (!t || c.dataset.search.includes(t))
        && (!d || c.dataset.distro === d)
        && (!g || c.dataset.tags.split(' ').includes(g))
        && (!s || c.dataset.status === s);
      c.hidden = !ok; visible += ok ? 1 : 0;
    }}
    empty.hidden = visible !== 0;
  }}
  for (const el of [q, distro, tag, status]) el.addEventListener('input', apply);
</script>
</body>
</html>
"""


def render_site(packages: list[dict], built_at: str) -> str:
    distros = sorted({p["distro"] for p in packages})
    tags = sorted({t for p in packages for t in p["tags"]})
    cards = "\n".join(render_card(p) for p in packages) or '<div class="empty">No packages registered yet.</div>'
    return PAGE_TEMPLATE.format(
        n_packages=len(packages),
        n_distros=len(distros),
        built_at=esc(built_at),
        distro_options="".join(f'<option value="{esc(d)}">{esc(d)}</option>' for d in distros),
        tag_options="".join(f'<option value="{esc(t)}">{esc(t)}</option>' for t in tags),
        cards=cards,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distributions-dir", default="distributions")
    parser.add_argument("--history-dir", default="", help="Path to the data branch's history/ dir")
    parser.add_argument("--out", default="_site")
    parser.add_argument("--built-at", default="", help="Build timestamp to stamp (default: blank)")
    args = parser.parse_args()

    registrations = load_distributions(Path(args.distributions_dir))
    history = load_history(Path(args.history_dir)) if args.history_dir else {}
    packages = build_packages(registrations, history)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_site(packages, args.built_at or "unknown"))
    n_records = sum(len(v) for v in history.values())
    print(f"built {len(packages)} package(s), {n_records} history record(s) -> {out_dir/'index.html'}")


if __name__ == "__main__":
    main()
