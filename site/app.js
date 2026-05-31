// Renders the Autoware Index browse view from data.json (written by build.py).
// All registry-supplied values go in via textContent, never innerHTML, so the
// page is XSS-safe even though registrations come from external repositories.

const STATUS_LABELS = { pass: "passing", fail: "failing", unknown: "not yet swept" };

// Tiny DOM helper. attrs: {class, text, href, title, ...}; children: nodes/strings.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    node.append(c);
  }
  return node;
}

function statusPill(status) {
  return el("span", { class: `pill pill-${status}`, text: STATUS_LABELS[status] || status });
}

function refText(ref) {
  return `${ref?.kind ?? ""} ${ref?.value ?? ""}`.trim();
}

function versionTable(versions) {
  if (!versions || versions.length === 0) {
    return el("p", { class: "muted", text: "No validation runs recorded yet." });
  }
  const head = el("thead", {}, el("tr", {},
    el("th", { text: "Autoware" }), el("th", { text: "Status" }),
    el("th", { text: "Ref tested" }), el("th", { text: "Commit" }), el("th", { text: "Tested" })));

  const body = el("tbody");
  for (const v of versions) {
    const sha = v.resolved_sha ? v.resolved_sha.slice(0, 12) : "—";
    const shaNode = (v.resolved_sha && v.actions_run_url)
      ? el("a", { href: v.actions_run_url, title: "View run", text: sha })
      : document.createTextNode(sha);
    body.append(el("tr", {},
      el("td", { text: `autoware ${v.autoware_version}` }),
      el("td", {}, statusPill(v.status || "unknown")),
      el("td", {}, el("code", { text: refText(v.ref_at_test) })),
      el("td", {}, el("code", {}, shaNode)),
      el("td", { text: v.at ? v.at.slice(0, 10) : "—" })));
  }
  return el("table", { class: "versions" }, head, body);
}

function maintainers(list) {
  if (!list || list.length === 0) return null;
  const div = el("div", { class: "maintainers" }, "Maintainers: ");
  list.forEach((m, i) => {
    if (i > 0) div.append(", ");
    const name = m.name || m.github || "";
    div.append(m.github ? el("a", { href: `https://github.com/${m.github}`, text: name })
                        : document.createTextNode(name));
  });
  return div;
}

function searchBlob(pkg) {
  const names = (pkg.maintainers || []).map((m) => m.name || "").join(" ");
  return `${pkg.name} ${(pkg.tags || []).join(" ")} ${names}`.toLowerCase();
}

function card(pkg) {
  const status = pkg.current_status || "unknown";
  const badges = el("div", { class: "badges" },
    el("span", { class: "badge badge-distro", text: pkg.distro }),
    el("span", { class: "badge badge-gov", text: pkg.governance }),
    statusPill(status));
  if (status === "fail" && pkg.last_green) {
    badges.append(el("span", { class: "muted", text: ` · last green: autoware ${pkg.last_green}` }));
  }

  const tags = el("div", { class: "tags" });
  for (const t of pkg.tags || []) tags.append(el("span", { class: "tag", text: t }));

  const meta = el("div", { class: "meta" },
    el("a", { class: "repo", href: pkg.repository, text: pkg.repository }),
    el("span", { class: "muted" }, " · registered ref: ", el("code", { text: refText(pkg.ref) })));

  const details = el("details", {},
    el("summary", { text: "Compatibility history" }), versionTable(pkg.versions));

  return el("article", {
    class: "card",
    "data-name": pkg.name.toLowerCase(),
    "data-distro": pkg.distro,
    "data-status": status,
    "data-tags": (pkg.tags || []).join(" "),
    "data-search": searchBlob(pkg),
  },
    el("header", {}, el("h2", { text: pkg.name }), badges),
    tags, meta, maintainers(pkg.maintainers), details);
}

function options(select, values) {
  for (const v of values) select.append(el("option", { value: v, text: v }));
}

function renderStats(stats, packages, builtAt) {
  const nDistros = new Set(packages.map((p) => p.distro)).size;
  stats.append(
    el("span", {}, el("b", { text: String(packages.length) }), " packages"),
    el("span", {}, el("b", { text: String(nDistros) }), " distributions"),
    el("span", {}, "built ", el("b", { text: builtAt || "unknown" })));
}

function wireFilters() {
  const q = document.getElementById("q");
  const distro = document.getElementById("distro");
  const tag = document.getElementById("tag");
  const status = document.getElementById("status");
  const empty = document.getElementById("empty");
  const cards = [...document.querySelectorAll(".card")];

  function apply() {
    const t = q.value.trim().toLowerCase();
    const d = distro.value, g = tag.value, s = status.value;
    let visible = 0;
    for (const c of cards) {
      const ok = (!t || c.dataset.search.includes(t))
        && (!d || c.dataset.distro === d)
        && (!g || c.dataset.tags.split(" ").includes(g))
        && (!s || c.dataset.status === s);
      c.hidden = !ok;
      visible += ok ? 1 : 0;
    }
    empty.hidden = visible !== 0;
  }
  for (const elm of [q, distro, tag, status]) elm.addEventListener("input", apply);
}

async function main() {
  const cardsEl = document.getElementById("cards");
  let data;
  try {
    const res = await fetch("data.json", { cache: "no-cache" });
    data = await res.json();
  } catch (err) {
    cardsEl.append(el("div", { class: "empty", text: "Could not load data.json." }));
    return;
  }

  const packages = data.packages || [];
  renderStats(document.getElementById("stats"), packages, data.built_at);

  if (packages.length === 0) {
    cardsEl.append(el("div", { class: "empty", text: "No packages registered yet." }));
    return;
  }

  options(document.getElementById("distro"), [...new Set(packages.map((p) => p.distro))].sort());
  options(document.getElementById("tag"),
    [...new Set(packages.flatMap((p) => p.tags || []))].sort());

  for (const pkg of packages) cardsEl.append(card(pkg));
  wireFilters();
}

main();
