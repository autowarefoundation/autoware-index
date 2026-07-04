// Renders the Autoware Index browse view from data.json (written by build.py)
// and the "repos builder" cart. All registry-supplied values go in via
// textContent, never innerHTML, so the page is XSS-safe even though
// registrations come from external repositories.
//
// The .repos file and the aw-index-cli command are produced by compose.mjs,
// the canonical composition module vendored from the aw-index-cli repo, so the
// site and the CLI never diverge (see site/compose.mjs and the drift-check CI).

import { composeReposFile } from "./compose.mjs";

const STATUS_LABELS = { pass: "passing", fail: "failing", unknown: "not yet swept" };
const CART_STORAGE_KEY = "awx-cart";
const REGISTRY_SOURCE = "autowarefoundation/autoware-index@main";

// Module state. `carts` is per-distro so switching the rosdistro picker swaps
// carts without losing the others. `pkgIndex` resolves a cart entry (a name)
// back to its full package object.
const state = {
  packages: [],
  pkgIndex: new Map(), // `${distro}/${name}` -> pkg
  siblings: new Map(), // repoKey -> count (for the monorepo note)
  carts: new Map(), // distro -> Set<name>
  distributions: new Map(), // distro -> reconstructed distribution dict (memoized)
  tagSummaries: new Map(), // tag id -> one-line summary (vocabulary tooltips)
  vocabulary: null, // data.json's tag_vocabulary block (groups + live tags)
  active: "jazzy",
  // Tag rail selection. Multiple tags combine as OR (union): that is the
  // faceted-browsing convention, and with 1-5 tags per package an AND
  // intersection would usually be empty in a registry this size. Empty set
  // = all packages.
  activeTags: new Set(),
};

// wireFilters installs the real filter pass here so the tag rail and the
// distro picker can re-apply it without synthesizing input events.
let applyFilters = () => {};

// Package/distro names are `^[a-z][a-z0-9_]*$`, so "/" is a safe separator.
const keyOf = (distro, name) => `${distro}/${name}`;

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

// Inline SVG icon (plus / check) for the circular cart toggle. Built with
// createElementNS so it stays in the SVG namespace; no innerHTML.
const SVG_NS = "http://www.w3.org/2000/svg";
function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "15");
  svg.setAttribute("height", "15");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", name === "check" ? "M3.5 8.5l3 3 6-7" : "M8 3.25v9.5M3.25 8h9.5");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);
  return svg;
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
  const head = el(
    "thead",
    {},
    el(
      "tr",
      {},
      el("th", { text: "Autoware" }),
      el("th", { text: "Status" }),
      el("th", { text: "Ref tested" }),
      el("th", { text: "Commit" }),
      el("th", { text: "Tested" }),
    ),
  );

  const body = el("tbody");
  for (const v of versions) {
    const sha = v.resolved_sha ? v.resolved_sha.slice(0, 12) : "-";
    const shaNode =
      v.resolved_sha && v.actions_run_url
        ? el("a", { href: v.actions_run_url, title: "View run", text: sha })
        : document.createTextNode(sha);
    body.append(
      el(
        "tr",
        {},
        el("td", { text: `autoware ${v.autoware_version}` }),
        el("td", {}, statusPill(v.status || "unknown")),
        el("td", {}, el("code", { text: refText(v.ref_at_test) })),
        el("td", {}, el("code", {}, shaNode)),
        el("td", { text: v.at ? v.at.slice(0, 10) : "-" }),
      ),
    );
  }
  return el("table", { class: "versions" }, head, body);
}

function maintainers(list) {
  if (!list || list.length === 0) return null;
  const div = el("div", { class: "maintainers" }, "Maintainers: ");
  list.forEach((m, i) => {
    if (i > 0) div.append(", ");
    const name = m.name || m.github || "";
    div.append(
      m.github
        ? el("a", { href: `https://github.com/${m.github}`, text: name })
        : document.createTextNode(name),
    );
  });
  return div;
}

function searchBlob(pkg) {
  const names = (pkg.maintainers || []).map((m) => m.name || "").join(" ");
  return `${pkg.name} ${pkg.description || ""} ${(pkg.tags || []).join(" ")} ${names} ${pkg.repository || ""} ${pkg.repo_name || ""}`.toLowerCase();
}

// (distro, repo_name) -> number of registered packages from that repository,
// so a card can say it ships with siblings from the same repo. The count map
// and the per-card lookup MUST build identical keys, hence one shared helper.
function repoKey(pkg) {
  return `${pkg.distro} ${pkg.repo_name || pkg.repository || ""}`;
}

function repoSiblingCounts(packages) {
  const counts = new Map();
  for (const p of packages) {
    counts.set(repoKey(p), (counts.get(repoKey(p)) || 0) + 1);
  }
  return counts;
}

function card(pkg, siblingCount) {
  const status = pkg.current_status || "unknown";
  const badges = el(
    "div",
    { class: "badges" },
    el("span", { class: "badge badge-distro", text: pkg.distro }),
    el("span", { class: "badge badge-gov", text: pkg.governance }),
    // A quiet outline pill (same family as the governance badge), not loose
    // text; a dangling fragment would break the band's pill row. It leads
    // into the status verdict, which always closes the row. el() skips null.
    status === "fail" && pkg.last_green
      ? el("span", {
          class: "badge",
          text: `last green ${pkg.last_green}`,
          title: `Last passing validation was against Autoware ${pkg.last_green}`,
        })
      : null,
    statusPill(status),
  );

  const toggle = el("button", {
    class: "cart-toggle",
    type: "button",
    "aria-pressed": "false",
    "aria-label": `Add ${pkg.name} to the repos builder`,
    title: "Add to repos",
    "data-pkg": pkg.name,
    "data-distro": pkg.distro,
  });
  renderToggle(toggle, false);

  const tags = el("div", { class: "tags" });
  for (const t of pkg.tags || []) {
    // The vocabulary summary doubles as the chip tooltip; el() skips null.
    tags.append(el("span", { class: "tag", text: t, title: state.tagSummaries.get(t) }));
  }

  const meta = el(
    "div",
    { class: "meta" },
    el("a", { class: "repo", href: pkg.repository, text: pkg.repository }),
    el("span", { class: "muted" }, " · registered ref: ", el("code", { text: refText(pkg.ref) })),
  );
  if (siblingCount > 1) {
    meta.append(
      el("span", {
        class: "muted",
        text: ` · one of ${siblingCount} registered packages from this repository`,
      }),
    );
  }

  const details = el(
    "details",
    {},
    el("summary", { text: "Compatibility history" }),
    versionTable(pkg.versions),
  );

  return el(
    "article",
    {
      class: "card",
      "data-name": pkg.name.toLowerCase(),
      "data-distro": pkg.distro,
      "data-status": status,
      "data-tags": (pkg.tags || []).join(" "),
      "data-search": searchBlob(pkg),
    },
    el(
      "header",
      {},
      el("h2", { text: pkg.name }),
      // One classification band under the name: the domain tag chips on
      // the left (wrapping onto further lines when there are many), the
      // registry pills (distro, governance, status) pinned at the right.
      el("div", { class: "card-head-actions" }, tags, badges, toggle),
    ),
    pkg.description ? el("p", { class: "description", text: pkg.description }) : null,
    meta,
    maintainers(pkg.maintainers),
    details,
  );
}

function options(select, values) {
  for (const v of values) select.append(el("option", { value: v, text: v }));
}

// Grouped tag facets from the vocabulary in data.json: one entry per group
// in vocabulary order, holding only the tags actually in use with their live
// counts and summaries. When the vocabulary block is absent (a cached
// pre-vocabulary data.json served next to a newer app.js), fall back to one
// flat untitled group of the observed union.
function tagGroups(packages, vocabulary) {
  const counts = new Map();
  for (const p of packages) {
    for (const t of p.tags || []) counts.set(t, (counts.get(t) || 0) + 1);
  }
  const asTag = (id, summary) => ({ id, count: counts.get(id), summary });
  if (!vocabulary || !Array.isArray(vocabulary.groups) || !Array.isArray(vocabulary.tags)) {
    const ids = [...counts.keys()].sort();
    return ids.length ? [{ title: null, tags: ids.map((id) => asTag(id, null)) }] : [];
  }
  const groups = [];
  const known = new Set();
  for (const group of vocabulary.groups) {
    const inUse = vocabulary.tags.filter((t) => t.group === group.id && counts.has(t.id));
    for (const t of inUse) known.add(t.id);
    if (inUse.length === 0) continue;
    groups.push({ title: group.title, tags: inUse.map((t) => asTag(t.id, t.summary)) });
  }
  // In-use tags absent from the vocabulary (mismatched --tags-file build)
  // must stay filterable; their chips render on cards regardless.
  const leftovers = [...counts.keys()].filter((t) => !known.has(t)).sort();
  if (leftovers.length) {
    groups.push({ title: "Other", tags: leftovers.map((id) => asTag(id, null)) });
  }
  return groups;
}

// The "all packages" row uses the empty id; it reads as pressed exactly when
// no tag is selected, and clicking it clears the whole selection.
function tagPressed(id) {
  return id === "" ? state.activeTags.size === 0 : state.activeTags.has(id);
}

// One rail row: tag name left, package count right.
function tagRow({ id, label, count, summary }) {
  return el(
    "button",
    {
      class: "tagbar-tag",
      type: "button",
      "data-tag": id,
      "aria-pressed": tagPressed(id) ? "true" : "false",
      title: summary, // el() skips null
    },
    el("span", { class: "tagbar-name", text: label }),
    el("span", { class: "tagbar-count", text: String(count) }),
  );
}

// (Re)build the tag rail scoped to the ACTIVE distro. The card view is
// always filtered to one distro, so global counts would overstate; counts
// must be recomputed whenever the distro picker changes. Selected tags
// still in use in the new distro are kept; the rest are dropped (the
// caller re-applies filters when that changed the selection).
function renderTagRail() {
  // The rail ships hidden so the no-JS/failed-fetch/empty-registry states
  // show no dead panel; the first successful render reveals it (and the
  // .wrap grid gains its rail column via the :has() rule in styles.css).
  document.getElementById("tagbar").hidden = false;
  const rail = document.getElementById("tagbar-groups");
  rail.textContent = "";
  const scoped = state.packages.filter((p) => p.distro === state.active);
  const groups = tagGroups(scoped, state.vocabulary);
  const available = new Set(groups.flatMap((g) => g.tags.map((t) => t.id)));
  for (const t of [...state.activeTags]) {
    if (!available.has(t)) state.activeTags.delete(t);
  }

  rail.append(tagRow({ id: "", label: "all packages", count: scoped.length, summary: null }));
  for (const group of groups) {
    if (group.title) rail.append(el("h3", { class: "tagbar-group", text: group.title }));
    for (const t of group.tags) rail.append(tagRow({ ...t, label: t.id }));
  }
}

function wireTagRail() {
  document.getElementById("tagbar").addEventListener("click", (e) => {
    const btn = e.target.closest(".tagbar-tag");
    if (!btn) return;
    const id = btn.dataset.tag;
    if (id === "") state.activeTags.clear();
    else if (state.activeTags.has(id)) state.activeTags.delete(id);
    else state.activeTags.add(id);
    for (const b of document.querySelectorAll(".tagbar-tag")) {
      b.setAttribute("aria-pressed", tagPressed(b.dataset.tag) ? "true" : "false");
    }
    applyFilters();
  });
}

function renderStats(stats, packages, builtAt) {
  const nDistros = new Set(packages.map((p) => p.distro)).size;
  stats.append(
    el("span", {}, el("b", { text: String(packages.length) }), " packages"),
    el("span", {}, el("b", { text: String(nDistros) }), " distributions"),
    el("span", {}, "built ", el("b", { text: builtAt || "unknown" })),
  );
}

function wireFilters() {
  const q = document.getElementById("q");
  const distro = document.getElementById("distro");
  const status = document.getElementById("status");
  const empty = document.getElementById("empty");
  const cards = [...document.querySelectorAll(".card")];

  applyFilters = function apply() {
    const t = q.value.trim().toLowerCase();
    const d = distro.value,
      g = state.activeTags,
      s = status.value;
    let visible = 0;
    for (const c of cards) {
      const ok =
        (!t || c.dataset.search.includes(t)) &&
        (!d || c.dataset.distro === d) &&
        (!g.size || c.dataset.tags.split(" ").some((tag) => g.has(tag))) &&
        (!s || c.dataset.status === s);
      c.hidden = !ok;
      visible += ok ? 1 : 0;
    }
    empty.hidden = visible !== 0;
  };
  for (const elm of [q, distro, status]) elm.addEventListener("input", applyFilters);
  // Paint the initial (rosdistro-scoped) view; the picker now defaults to a
  // single distro instead of "All distros", so the first render must filter.
  applyFilters();
}

// --- Cart -----------------------------------------------------------------

function getCart(distro) {
  let set = state.carts.get(distro);
  if (!set) {
    set = new Set();
    state.carts.set(distro, set);
  }
  return set;
}

function loadStoredCart() {
  try {
    const raw = localStorage.getItem(CART_STORAGE_KEY);
    if (!raw) return {};
    const data = JSON.parse(raw);
    return (data && data.carts) || {};
  } catch (err) {
    return {};
  }
}

function saveCart() {
  try {
    const carts = {};
    for (const [distro, set] of state.carts) {
      if (set.size) carts[distro] = [...set];
    }
    localStorage.setItem(CART_STORAGE_KEY, JSON.stringify({ v: 1, carts }));
  } catch (err) {
    // Storage unavailable (private mode / disabled): keep the cart in memory.
  }
}

// Re-hydrate names from storage, dropping any that no longer exist in data.json
// (e.g. a package was removed after this cart was last saved).
function hydrateCart() {
  const stored = loadStoredCart();
  for (const [distro, names] of Object.entries(stored)) {
    if (!Array.isArray(names)) continue;
    const set = getCart(distro);
    for (const name of names) {
      if (state.pkgIndex.has(keyOf(distro, name))) set.add(name);
    }
  }
}

// Rebuild the nested distribution dict (compose.mjs's input shape) for one
// distro from the flat data.json packages. url/ref are identical across a
// repository's packages by the one-ref-per-repository invariant.
function buildDistribution(distro) {
  const repositories = {};
  for (const p of state.packages) {
    if (p.distro !== distro) continue;
    const key = p.repo_name || p.repository;
    if (!repositories[key]) {
      repositories[key] = { url: p.repository, ref: p.ref, packages: {} };
    }
    repositories[key].packages[p.name] = { tags: p.tags || [] };
  }
  return { schema_version: "2", ros_distro: distro, repositories };
}

function distributionFor(distro) {
  if (!state.distributions.has(distro)) {
    state.distributions.set(distro, buildDistribution(distro));
  }
  return state.distributions.get(distro);
}

// Fixed-size icon button; the words live in the title tooltip + aria-label so
// nothing reflows on hover.
function renderToggle(btn, inCart) {
  btn.textContent = "";
  btn.append(icon(inCart ? "check" : "plus"));
}

function syncToggle(btn) {
  const inCart = getCart(btn.dataset.distro).has(btn.dataset.pkg);
  const pkg = btn.dataset.pkg;
  btn.setAttribute("aria-pressed", inCart ? "true" : "false");
  btn.setAttribute(
    "aria-label",
    inCart ? `Remove ${pkg} from the repos builder` : `Add ${pkg} to the repos builder`,
  );
  btn.setAttribute("title", inCart ? "Added (click to remove)" : "Add to repos");
  renderToggle(btn, inCart);
}

function syncCardToggle(distro, name) {
  const btn = document.querySelector(`.cart-toggle[data-distro="${distro}"][data-pkg="${name}"]`);
  if (btn) syncToggle(btn);
}

function syncAllToggles() {
  for (const btn of document.querySelectorAll(".cart-toggle")) syncToggle(btn);
}

function cartRepoBlock(group) {
  const block = el("div", { class: "cart-repo" });
  block.append(
    el(
      "div",
      { class: "cart-repo-head" },
      el("code", { class: "cart-repo-name", text: group.repo }),
      el("span", { class: "muted cart-repo-ref", text: refText(group.ref) }),
    ),
  );
  for (const name of group.names) {
    block.append(
      el(
        "div",
        { class: "cart-pkg" },
        el("span", { text: name }),
        el("button", {
          class: "cart-remove",
          type: "button",
          "data-pkg": name,
          "aria-label": `Remove ${name}`,
          text: "Remove",
        }),
      ),
    );
  }
  if (group.siblingTotal > group.names.length) {
    block.append(
      el("p", {
        class: "cart-note",
        text: `Adds the whole ${group.repo} repository: ${group.siblingTotal} registered packages travel together (one clone).`,
      }),
    );
  }
  return block;
}

function renderCart() {
  const distro = state.active;
  document.getElementById("cart-distro").textContent = distro;

  const items = [...getCart(distro)]
    .map((name) => state.pkgIndex.get(keyOf(distro, name)))
    .filter(Boolean);

  const itemsEl = document.getElementById("cart-items");
  itemsEl.textContent = "";
  const emptyEl = document.getElementById("cart-empty");
  const exportEl = document.getElementById("export");

  if (items.length === 0) {
    emptyEl.hidden = false;
    exportEl.hidden = true;
    return;
  }
  emptyEl.hidden = true;

  const groups = new Map();
  for (const p of items) {
    const key = p.repo_name || p.repository;
    if (!groups.has(key)) {
      groups.set(key, {
        repo: key,
        ref: p.ref,
        names: [],
        siblingTotal: state.siblings.get(repoKey(p)) || 1,
      });
    }
    groups.get(key).names.push(p.name);
  }
  for (const key of [...groups.keys()].sort()) {
    const group = groups.get(key);
    group.names.sort();
    itemsEl.append(cartRepoBlock(group));
  }

  renderExport(distro, items);
}

function renderExport(distro, items) {
  const names = [...new Set(items.map((p) => p.name))].sort();
  let reposText;
  try {
    reposText = composeReposFile(distributionFor(distro), {
      rosDistro: distro,
      source: REGISTRY_SOURCE,
      packages: names,
    });
  } catch (err) {
    reposText = `# could not generate .repos: ${err.message}`;
  }
  document.querySelector("#repos-out code").textContent = reposText;
  // The compose command that regenerates this .repos (writes autoware-index.repos
  // in the workspace root). Built here rather than via compose.mjs's
  // composeCommand so it can be shown without the `--stdout | vcs import src`
  // one-liner and wrapped one argument/package per line for readability in the
  // sidebar; the vendored compose.mjs stays byte-identical to the release.
  const bs = String.fromCharCode(92); // backslash for shell line-continuation
  const cmdLines = ["aw-index-cli compose", ` --rosdistro ${distro}`, " --packages", ...names];
  document.querySelector("#cmd-out code").textContent = cmdLines.join(` ${bs}\n`);
  document.getElementById("export").hidden = false;
}

function fallbackCopy(text) {
  const ta = el("textarea", {});
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.append(ta);
  ta.select();
  try {
    document.execCommand("copy");
  } finally {
    ta.remove();
  }
}

async function copyOutput(which, statusEl) {
  const sel = which === "cmd" ? "#cmd-out code" : "#repos-out code";
  const text = document.querySelector(sel).textContent;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      fallbackCopy(text);
    }
    statusEl.textContent = "Copied";
  } catch (err) {
    try {
      fallbackCopy(text);
      statusEl.textContent = "Copied";
    } catch (err2) {
      statusEl.textContent = "Copy failed; select the text and copy manually";
    }
  }
}

function downloadRepos() {
  const text = document.querySelector("#repos-out code").textContent;
  const blob = new Blob([text], { type: "text/yaml" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: "autoware-index.repos" });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function wireRosdistro() {
  const distro = document.getElementById("distro");
  const distros = [...new Set(state.packages.map((p) => p.distro))].sort();
  state.active = distros.includes("jazzy") ? "jazzy" : distros[0] || "jazzy";
  distro.value = state.active;
  // Card visibility is already handled by wireFilters' own input listener on
  // #distro; here we swap which cart the sidebar shows and rebuild the tag
  // rail's per-distro counts. If the rebuild resets the tag selection,
  // re-apply the filters so cards match the rail.
  distro.addEventListener("change", () => {
    state.active = distro.value;
    const before = [...state.activeTags].sort().join(" ");
    renderTagRail();
    if ([...state.activeTags].sort().join(" ") !== before) applyFilters();
    renderCart();
  });
}

function wireCart() {
  document.getElementById("cards").addEventListener("click", (e) => {
    const btn = e.target.closest(".cart-toggle");
    if (!btn) return;
    const { distro, pkg } = btn.dataset;
    const set = getCart(distro);
    if (set.has(pkg)) set.delete(pkg);
    else set.add(pkg);
    saveCart();
    syncToggle(btn);
    if (distro === state.active) renderCart();
  });

  document.getElementById("cart").addEventListener("click", (e) => {
    const remove = e.target.closest(".cart-remove");
    if (remove) {
      const name = remove.dataset.pkg;
      getCart(state.active).delete(name);
      saveCart();
      renderCart();
      syncCardToggle(state.active, name);
      return;
    }
    if (e.target.closest("#cart-clear")) {
      getCart(state.active).clear();
      saveCart();
      renderCart();
      syncAllToggles();
      return;
    }
    const copyBtn = e.target.closest("[data-copy]");
    if (copyBtn) {
      copyOutput(copyBtn.dataset.copy, document.getElementById("copy-status"));
      return;
    }
    if (e.target.closest("#download-repos")) downloadRepos();
  });
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
  state.packages = packages;
  for (const p of packages) state.pkgIndex.set(keyOf(p.distro, p.name), p);
  state.siblings = repoSiblingCounts(packages);
  state.vocabulary = data.tag_vocabulary || null;
  const vocabTags = Array.isArray(state.vocabulary?.tags) ? state.vocabulary.tags : [];
  for (const t of vocabTags) state.tagSummaries.set(t.id, t.summary);

  renderStats(document.getElementById("stats"), packages, data.built_at);

  if (packages.length === 0) {
    cardsEl.append(el("div", { class: "empty", text: "No packages registered yet." }));
    return;
  }

  options(document.getElementById("distro"), [...new Set(packages.map((p) => p.distro))].sort());

  for (const pkg of packages) {
    cardsEl.append(card(pkg, state.siblings.get(repoKey(pkg)) || 1));
  }

  hydrateCart();
  wireRosdistro(); // sets the default distro before wireFilters paints
  renderTagRail(); // needs state.active, so after wireRosdistro
  wireFilters();
  wireTagRail();
  wireCart();
  syncAllToggles();
  renderCart();
}

main();
