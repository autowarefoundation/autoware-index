// Registration helper: builds one distributions/<distro>.yaml `repositories:`
// entry from a guided form, previews it as live YAML, and mirrors the PR
// gate's checks (check-jsonschema shape, check_tags vocabulary, check_refs
// uniqueness/maintainers/ref-resolution) client-side so a registration
// arrives at the pull request already green.
//
// Same safety rule as app.js: every user- or API-supplied value enters the
// DOM via textContent, never innerHTML.
//
// GitHub-hosted repositories get auto-discovery through the public GitHub
// API (repo info, branches/tags for the ref picker, a tree scan that finds
// and parses package.xml files). Everything degrades to manual entry: the
// API is a convenience, CI remains the authority.

// --- Constants mirroring the repo's validators ------------------------------

// schema/distribution.schema.json propertyNames patterns.
const REPO_KEY_RE = /^[a-z][a-z0-9_-]*$/;
const PKG_NAME_RE = /^[a-z][a-z0-9_]*$/;
// scripts/check_refs.py SHA_RE + PLACEHOLDER_NAMES.
const SHA_RE = /^[0-9a-f]{40}$/;
const PLACEHOLDER_NAMES = new Set(["tbd", "todo", "n/a", "na", "none", "xxx", ""]);

const REGISTRY_REPO = "autowarefoundation/autoware-index";
const GH_API = "https://api.github.com";
const MAX_SCAN_FILES = 40; // package.xml files fetched per scan (rate-limit friendly)
const MAX_HANDLE_LOOKUPS = 12; // maintainer-email -> GitHub-handle lookups per scan
// Sentinel option in the ref dropdown that switches to free-text entry.
// Starts with a slash+space, which no real git ref name can.
const CUSTOM_REF = "/ custom";

// --- Tiny DOM helper (same shape as app.js) ---------------------------------

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

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// --- State -------------------------------------------------------------------

let pkgSeq = 0;

const state = {
  distros: ["jazzy"],
  distro: "jazzy",
  vocabulary: { groups: [], tags: [] },
  tagInfo: new Map(), // id -> {group, summary, disambiguation?}
  // Per-distro index of what data.json says is already registered, so the
  // uniqueness gate mirrors check_refs.py before the PR exists.
  existing: new Map(), // distro -> {urls: Map(canon->repo_name), repoNames: Set, packages: Map(name->repo_name)}
  form: {
    url: "",
    repoName: "",
    governance: "community",
    refKind: "tag",
    refValue: "",
    packages: [], // {id, name, tags: [], description: "", maintainers: [] (override; empty = inherit), upstream: {description, path} | null}
    maintainers: [blankMaintainer()],
  },
  github: {
    slug: null, // {owner, repo} for github.com clone URLs only
    status: "idle", // idle | loading | ok | notfound | ratelimited | error | nonGithub | pageurl
    info: null, // {default_branch, archived, description}
    tags: [],
    branches: [],
    listsMaybeTruncated: { tags: false, branches: false },
    inspectSeq: 0,
    inspected: "", // "owner/repo" already fetched
    refCheck: { key: "", status: "idle" }, // idle | checking | ok | missing
    scan: { key: "", status: "idle", found: [], maintainers: [], note: "" },
    rateLimit: null, // {limit, reset} from the 403's x-ratelimit-* headers
  },
  ui: {
    repoNameTouched: false,
    refTouched: false,
    refAutofilled: false,
    refCustom: false, // user chose "Other: type a name…" over the dropdown
    governanceTouched: false,
    lastLines: [],
  },
};

function blankMaintainer() {
  return { name: "", email: "", github: "" };
}

// --- Mirrors of registry_load.canonical_url / check_refs rules ---------------

// Keep in lockstep with registry_load.canonical_url: fold scheme, scp form,
// userinfo, port, trailing slash, .git suffix, and case into host/path.
function canonicalUrl(url) {
  const u = url.trim();
  const scp = u.match(/^(?:[^@/]+@)?([^:/@]+):(?!\/\/)(.+)$/);
  let host = "";
  let path = u;
  if (u.includes("://")) {
    try {
      const parsed = new URL(u);
      host = parsed.hostname || "";
      path = parsed.pathname;
    } catch {
      host = "";
      path = u;
    }
  } else if (scp) {
    host = scp[1];
    path = scp[2];
  }
  let combined = host ? `${host}/${path.replace(/^\/+/, "")}` : path;
  combined = combined.replace(/\/+$/, "");
  combined = combined.replace(/\.git$/, "");
  return combined.toLowerCase();
}

// {owner, repo} when the URL is exactly a github.com repository (clone URL,
// not a /tree/... page URL; those would pass API checks here but fail CI's
// git ls-remote, so they must NOT get the auto-discovery treatment).
function parseGithub(url) {
  const canon = canonicalUrl(url);
  const m = canon.match(/^(?:www\.)?github\.com\/([^/]+)\/([^/]+)$/);
  if (!m) return null;
  return { owner: m[1], repo: m[2] };
}

function isGithubPageUrl(url) {
  const canon = canonicalUrl(url);
  return /^(?:www\.)?github\.com\/[^/]+\/[^/]+\/.+/.test(canon);
}

// github.com with no /owner/repo path (e.g. an org page). CI's git ls-remote
// hard-fails on these, so they must be a hard fail here too, not the benign
// "non-GitHub host" note.
function isGithubIncompleteUrl(url) {
  return /^(?:www\.)?github\.com(\/[^/]*)?$/.test(canonicalUrl(url));
}

// A git remote the registry can actually use: an absolute http(s) or git://
// URL with a real-looking host and a repository path. SSH remotes (ssh://
// or the scp-like user@host:path form) are well-formed for git but the
// sweep and CI clone anonymously, so they are rejected with the https
// equivalent offered as a one-click fix. Returns null when the URL is fine.
function gitUrlProblem(url) {
  const u = url.trim();
  if (!u) return null;
  const scp = u.match(/^(?:[\w.-]+@)([\w.-]+):(?!\/\/)(.+)$/);
  if (scp) {
    return {
      msg: "SSH remotes can't be cloned anonymously by the sweep or CI; use the https:// URL.",
      fix: httpsEquivalent(scp[1], scp[2]),
    };
  }
  let parsed;
  try {
    parsed = new URL(u);
  } catch {
    return { msg: "This isn't a URL; paste the repository's https:// clone URL." };
  }
  const scheme = parsed.protocol.replace(/:$/, "").toLowerCase();
  if (scheme === "ssh" || scheme === "git+ssh") {
    return {
      msg: "SSH remotes can't be cloned anonymously by the sweep or CI; use the https:// URL.",
      fix: httpsEquivalent(parsed.hostname, parsed.pathname),
    };
  }
  if (!["http", "https", "git"].includes(scheme)) {
    return { msg: `“${scheme}:” isn't a fetchable git remote; paste the https:// clone URL.` };
  }
  if (!/^[\w-]+(\.[\w-]+)+$/.test(parsed.hostname)) {
    return { msg: "The host doesn't look like a real domain." };
  }
  if (parsed.pathname.replace(/\/+$/, "").length < 2) {
    return { msg: "The URL is missing the repository path." };
  }
  return null;
}

function httpsEquivalent(host, path) {
  const clean = path
    .replace(/^\/+/, "")
    .replace(/\.git$/, "")
    .replace(/\/+$/, "");
  return clean && /^[\w-]+(\.[\w-]+)+$/.test(host) ? `https://${host}/${clean}` : null;
}

function placeholderProblems(m) {
  const problems = [];
  const name = (m.name || "").trim().toLowerCase();
  const github = (m.github || "").trim().toLowerCase();
  const email = (m.email || "").trim().toLowerCase();
  if (m.name && PLACEHOLDER_NAMES.has(name)) problems.push(`placeholder name “${m.name.trim()}”`);
  if (m.github && PLACEHOLDER_NAMES.has(github))
    problems.push(`placeholder GitHub handle “${m.github.trim()}”`);
  if (email.endsWith("@example.com") || email.endsWith("@example.org"))
    problems.push(`placeholder email “${m.email.trim()}”`);
  return problems;
}

function maintainerComplete(m) {
  return Boolean(m.name.trim() && m.email.trim() && m.github.trim());
}

function maintainerEmpty(m) {
  return !m.name.trim() && !m.email.trim() && !m.github.trim();
}

// --- YAML emission ------------------------------------------------------------

// Plain (unquoted) YAML scalar when clearly safe; otherwise a JSON string,
// which is a valid YAML double-quoted scalar. The repo's yamllint config
// enforces quoted-strings: only-when-needed, so quoting happens EXACTLY when
// the plain form would break: characters a block-context plain scalar can't
// carry (leading indicators, ": ", " #", control whitespace, trailing
// space/colon), or values PyYAML's implicit resolvers would type as
// something other than a string: bool/null words, every numeric form
// (decimal, hex/octal/binary, underscore-grouped, sexagesimal like 12:30,
// .inf/.nan), and timestamps. URLs and unicode names stay plain.
function yamlScalar(value) {
  const v = String(value);
  const startsBad = /^[\s\-?:,[\]{}#&*!|>'"%@`]/.test(v);
  const containsBad = /: /.test(v) || / #/.test(v) || /[\t\n\r]/.test(v) || /[\s:]$/.test(v);
  const looksTyped =
    /^(true|false|yes|no|on|off|null|~)$/i.test(v) ||
    /^[+-]?(\.[\d_]+|\d[\d_]*\.?[\d_]*)([eE][+-]?\d+)?$/.test(v) ||
    /^[+-]?0[xbo][0-9a-fA-F_]+$/i.test(v) ||
    /^[+-]?\d[\d_]*(:[0-5]?\d)+(\.[\d_]*)?$/.test(v) ||
    /^[+-]?\.(inf|nan)$/i.test(v) ||
    /^\d{4}-\d{1,2}-\d{1,2}([Tt ].+)?$/.test(v);
  const plainSafe = v.length > 0 && !startsBad && !containsBad && !looksTyped;
  return plainSafe ? v : JSON.stringify(v);
}

function wrapWords(text, width) {
  const words = text.split(" ");
  const lines = [];
  let line = "";
  for (const word of words) {
    if (line && line.length + 1 + word.length > width) {
      lines.push(line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function descriptionLines(desc, indent) {
  const text = desc.trim().replace(/\s+/g, " ");
  if (!text) return [];
  const inline = yamlScalar(text);
  if (text.length <= 60 && inline === text) {
    return [`${indent}description: ${text}`];
  }
  // Folded block scalar, matching the registry's house style for long text.
  const width = Math.max(40, 78 - indent.length - 2);
  return [`${indent}description: >-`, ...wrapWords(text, width).map((l) => `${indent}  ${l}`)];
}

function maintainerLines(list, indent) {
  const lines = [];
  for (const m of list) {
    if (maintainerEmpty(m)) continue;
    const fields = [];
    if (m.name.trim()) fields.push(["name", m.name.trim()]);
    if (m.email.trim()) fields.push(["email", m.email.trim()]);
    if (m.github.trim()) fields.push(["github", m.github.trim()]);
    fields.forEach(([key, value], i) => {
      const lead = i === 0 ? `${indent}- ` : `${indent}  `;
      lines.push(`${lead}${key}: ${yamlScalar(value)}`);
    });
  }
  return lines;
}

// The entry as YAML lines (2-space base indent, ready to append under
// `repositories:`). Missing required values render as comment hints naming
// the station that provides them; the buffer doubles as the progress view.
// Map KEYS are emitted only once they match their schema pattern (so pasted
// or scanned garbage can never inject YAML structure through a key), and go
// through yamlScalar so bool-word names like `no` stay strings.
function entryLines() {
  const f = state.form;
  const lines = [];
  const key = f.repoName.trim();
  if (key && REPO_KEY_RE.test(key)) {
    lines.push(`  ${yamlScalar(key)}:`);
  } else if (key) {
    lines.push(`  # entry name isn't valid yet (station 1)`);
  } else {
    lines.push(`  # <entry name>:  (station 1)`);
  }

  lines.push(f.url.trim() ? `    url: ${yamlScalar(f.url.trim())}` : `    url: # station 1`);

  lines.push(`    ref:`);
  lines.push(`      kind: ${f.refKind}`);
  lines.push(
    f.refValue.trim()
      ? `      value: ${yamlScalar(f.refValue.trim())}`
      : `      value: # station 2`,
  );

  lines.push(`    governance: ${f.governance}`);

  lines.push(`    maintainers:`);
  const maintLines = maintainerLines(f.maintainers, "      ");
  if (maintLines.length) lines.push(...maintLines);
  else lines.push(`      # add at least one maintainer (station 4)`);

  lines.push(`    packages:`);
  const named = f.packages.filter((p) => PKG_NAME_RE.test(p.name.trim()));
  const unnamed = f.packages.length - named.length;
  if (!f.packages.length) {
    lines.push(`      # add at least one package (station 3)`);
  }
  for (const pkg of named) {
    lines.push(`      ${yamlScalar(pkg.name.trim())}:`);
    lines.push(`        tags:`);
    if (pkg.tags.length) {
      for (const t of pkg.tags) lines.push(`          - ${t}`);
    } else {
      lines.push(`          # pick at least one tag (station 3)`);
    }
    lines.push(...descriptionLines(pkg.description, "        "));
    const overrides = maintainerLines(pkg.maintainers, "          ");
    if (overrides.length) {
      lines.push(`        maintainers:`);
      lines.push(...overrides);
    }
  }
  if (unnamed > 0) {
    lines.push(`      # ${unnamed} package(s) still need a valid name (station 3)`);
  }
  return lines;
}

// --- Validation: the PR gates, mirrored --------------------------------------

function existingFor(distro) {
  return (
    state.existing.get(distro) || { urls: new Map(), repoNames: new Set(), packages: new Map() }
  );
}

function validate() {
  const f = state.form;
  const gh = state.github;
  const registered = existingFor(state.distro);
  const completeMaintainers = f.maintainers.filter(maintainerComplete);
  const gates = [];

  // Gate 1 (check-jsonschema): required fields + patterns.
  {
    const missing = [];
    const invalid = [];
    const urlProblem = gitUrlProblem(f.url);
    if (!f.url.trim()) missing.push("repository URL");
    else if (urlProblem) invalid.push(`repository URL: ${urlProblem.msg}`);
    if (!f.repoName.trim()) missing.push("entry name");
    else if (!REPO_KEY_RE.test(f.repoName.trim()))
      invalid.push("entry name must match [a-z][a-z0-9_-]*");
    if (!f.refValue.trim()) missing.push(`ref ${f.refKind}`);
    if (!f.packages.length) missing.push("at least one package");
    for (const pkg of f.packages) {
      const name = pkg.name.trim();
      if (!name) missing.push("a package name");
      else if (!PKG_NAME_RE.test(name))
        invalid.push(`package “${name}” must match [a-z][a-z0-9_]* (no hyphens)`);
      for (const m of pkg.maintainers) {
        if (!maintainerEmpty(m) && !maintainerComplete(m))
          missing.push(`name, email and GitHub handle for ${name || "a package"}'s override`);
      }
    }
    if (!completeMaintainers.length) missing.push("one complete maintainer");
    for (const m of f.maintainers) {
      if (!maintainerEmpty(m) && !maintainerComplete(m))
        missing.push("name, email and GitHub handle on every maintainer");
    }
    let status = "ok";
    let msg = "entry matches distribution.schema.json";
    if (invalid.length) {
      status = "fail";
      msg = invalid[0];
    } else if (missing.length) {
      status = "pending";
      msg = `waiting on: ${[...new Set(missing)].slice(0, 3).join(", ")}`;
    }
    gates.push({ id: "shape", name: "check-jsonschema", status, msg });
  }

  // Gate 2 (check_tags): at least one live tag per package; tool-only warns.
  {
    let status = "ok";
    let msg = "every package carries at least one tag from the vocabulary";
    const untagged = f.packages.filter((p) => p.name.trim() && !p.tags.length);
    const toolOnly = f.packages.filter(
      (p) => p.tags.length === 1 && p.tags[0] === "tool" && p.name.trim(),
    );
    if (!f.packages.length) {
      status = "pending";
      msg = "no packages yet";
    } else if (untagged.length) {
      status = "pending";
      msg = `pick tags for ${untagged[0].name.trim()}`;
    } else if (toolOnly.length) {
      status = "warn";
      msg = `“tool” is ${toolOnly[0].name.trim()}'s only tag; add the domain it serves`;
    }
    gates.push({ id: "tags", name: "check_tags", status, msg });
  }

  // Gate 3 (check_refs · uniqueness): URL, entry name, and package names must
  // be new to this distro (and package names unique within the entry).
  {
    let status = "ok";
    let msg = `nothing here collides with the ${state.distro} registry`;
    const problems = [];
    const canon = f.url.trim() ? canonicalUrl(f.url) : "";
    if (canon && registered.urls.has(canon)) {
      problems.push(
        `this repository is already registered as “${registered.urls.get(canon)}”; update that entry instead`,
      );
    }
    const key = f.repoName.trim();
    if (key && registered.repoNames.has(key)) {
      problems.push(`entry name “${key}” is taken in ${state.distro}`);
    }
    const seen = new Set();
    for (const pkg of f.packages) {
      const name = pkg.name.trim();
      if (!name) continue;
      if (registered.packages.has(name)) {
        problems.push(
          `package “${name}” is already registered by “${registered.packages.get(name)}”`,
        );
      }
      if (seen.has(name)) problems.push(`package “${name}” is listed twice`);
      seen.add(name);
    }
    if (problems.length) {
      status = "fail";
      msg = problems[0];
    } else if (!canon && !key) {
      status = "pending";
      msg = "waiting on station 1";
    }
    gates.push({ id: "unique", name: "check_refs · uniqueness", status, msg });
  }

  // Gate 4 (check_refs · maintainers): no placeholders, anywhere.
  {
    let status = "ok";
    let msg = "maintainers look real";
    const problems = [];
    for (const m of f.maintainers) problems.push(...placeholderProblems(m));
    for (const pkg of f.packages)
      for (const m of pkg.maintainers) problems.push(...placeholderProblems(m));
    if (problems.length) {
      status = "fail";
      msg = problems[0];
    } else if (!completeMaintainers.length) {
      status = "pending";
      msg = "waiting on station 4";
    }
    gates.push({ id: "maintainers", name: "check_refs · maintainers", status, msg });
  }

  // Gate 5 (check_refs · ref resolution). sha is format-only (like CI);
  // tag/branch resolve through the GitHub API when possible, else CI verifies.
  {
    let status = "pending";
    let msg = "waiting on station 2";
    const value = f.refValue.trim();
    if (value) {
      if (f.refKind === "sha") {
        if (SHA_RE.test(value)) {
          status = "ok";
          msg = "sha format is valid (the sweep's checkout verifies reachability)";
        } else {
          status = "fail";
          msg = "a sha ref must be exactly 40 lowercase hex characters";
        }
      } else if (gh.slug && gh.status === "ok") {
        const check = gh.refCheck;
        if (check.key !== refCheckKey()) {
          status = "pending";
          msg = "checking upstream…";
        } else if (check.status === "checking") {
          status = "pending";
          msg = "asking GitHub whether this ref exists…";
        } else if (check.status === "ok") {
          status = "ok";
          msg = `${f.refKind} “${value}” exists upstream`;
        } else if (check.status === "missing") {
          status = "fail";
          msg = `${f.refKind} “${value}” does not resolve upstream; git ls-remote will reject it`;
        }
      } else if (gh.status === "notfound") {
        status = "fail";
        msg = "repository not found on GitHub (private repositories can't be swept)";
      } else if (gh.status === "ratelimited") {
        status = "info";
        const rl = rateLimitInfo();
        msg = `GitHub API rate limit reached${rl ? ` (resets at ${rl.hhmm}, in ~${rl.mins} min)` : ""}; CI verifies the ref with git ls-remote`;
      } else if (gh.status === "pageurl") {
        status = "fail";
        msg = "that's a GitHub page URL; register the repository clone URL (no /tree/…)";
      } else if (gh.status === "incomplete") {
        status = "fail";
        msg =
          "the github.com URL is missing its /owner/repository path, so git ls-remote will fail";
      } else if (!gh.slug) {
        status = "info";
        msg = "can't check from the browser; CI verifies the ref with git ls-remote";
      } else if (gh.status === "error") {
        status = "info";
        msg = "couldn't reach the GitHub API; CI verifies the ref with git ls-remote";
      } else {
        status = "pending";
        msg = "looking up the repository…";
      }
    }
    gates.push({ id: "ref", name: "check_refs · ref resolves", status, msg });
  }

  const ready = gates.every((g) => g.status === "ok" || g.status === "warn" || g.status === "info");
  return { gates, ready };
}

// --- GitHub API --------------------------------------------------------------

async function ghJson(path) {
  let res;
  try {
    res = await fetch(`${GH_API}${path}`, {
      headers: { Accept: "application/vnd.github+json" },
    });
  } catch {
    return { ok: false, status: 0, data: null, rateLimited: false };
  }
  const rateLimited =
    (res.status === 403 || res.status === 429) && res.headers.get("x-ratelimit-remaining") === "0";
  if (rateLimited) {
    state.github.rateLimit = {
      limit: Number(res.headers.get("x-ratelimit-limit")) || 60,
      reset: Number(res.headers.get("x-ratelimit-reset")) || 0,
    };
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  return { ok: res.ok, status: res.status, data, rateLimited };
}

// Human-readable facts about the active rate limit, recomputed on every
// render so the countdown stays current. Null when the 403 carried no
// usable x-ratelimit-reset header.
function rateLimitInfo() {
  const rl = state.github.rateLimit;
  if (!rl?.reset) return null;
  const resetAt = new Date(rl.reset * 1000);
  return {
    limit: rl.limit,
    hhmm: resetAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    mins: Math.max(0, Math.ceil((resetAt.getTime() - Date.now()) / 60000)),
  };
}

function refCheckKey() {
  const gh = state.github;
  const slug = gh.slug ? `${gh.slug.owner}/${gh.slug.repo}` : "";
  return `${slug}|${state.form.refKind}|${state.form.refValue.trim()}`;
}

function scanKey() {
  const gh = state.github;
  const slug = gh.slug ? `${gh.slug.owner}/${gh.slug.repo}` : "";
  return `${slug}|${state.form.refValue.trim()}`;
}

// Everything learned from one repository must die with it: a URL edit that
// changes (or unsets) the slug clears the lists, the ref check, and the scan
// so gates never run on a previous repository's data.
function resetDiscovery(gh) {
  gh.info = null;
  gh.tags = [];
  gh.branches = [];
  gh.listsMaybeTruncated = { tags: false, branches: false };
  gh.inspected = "";
  gh.refCheck = { key: "", status: "idle" };
  gh.scan = { key: "", status: "idle", found: [], maintainers: [], note: "" };
}

async function inspectRepo() {
  const gh = state.github;
  // Bump FIRST: any newer invocation, including ones that resolve to a
  // non-GitHub or empty URL, must invalidate in-flight API responses.
  const seq = ++gh.inspectSeq;
  const slug = parseGithub(state.form.url);
  gh.slug = slug;
  if (!slug) {
    if (!state.form.url.trim()) gh.status = "idle";
    else if (isGithubIncompleteUrl(state.form.url)) gh.status = "incomplete";
    else if (isGithubPageUrl(state.form.url)) gh.status = "pageurl";
    else gh.status = "nonGithub";
    resetDiscovery(gh);
    updateRefDatalist();
    renderRefControl();
    refresh();
    return;
  }
  const slugKey = `${slug.owner}/${slug.repo}`;
  if (gh.inspected === slugKey && (gh.status === "ok" || gh.status === "notfound")) {
    refresh();
    return;
  }
  gh.status = "loading";
  resetDiscovery(gh);
  updateRefDatalist();
  renderRefControl();
  refresh();

  const [info, tags, branches] = await Promise.all([
    ghJson(`/repos/${slug.owner}/${slug.repo}`),
    ghJson(`/repos/${slug.owner}/${slug.repo}/tags?per_page=100`),
    ghJson(`/repos/${slug.owner}/${slug.repo}/branches?per_page=100`),
  ]);
  if (seq !== gh.inspectSeq) return; // a newer URL superseded this lookup

  if (info.rateLimited || tags.rateLimited || branches.rateLimited) {
    gh.status = "ratelimited";
    refresh();
    return;
  }
  if (info.status === 404) {
    gh.status = "notfound";
    gh.inspected = slugKey;
    refresh();
    return;
  }
  if (!info.ok) {
    gh.status = "error";
    refresh();
    return;
  }

  gh.status = "ok";
  gh.inspected = slugKey;
  gh.info = {
    default_branch: info.data?.default_branch || "main",
    archived: Boolean(info.data?.archived),
    description: info.data?.description || "",
  };
  gh.tags = Array.isArray(tags.data) ? tags.data.map((t) => t.name) : [];
  gh.branches = Array.isArray(branches.data) ? branches.data.map((b) => b.name) : [];
  gh.listsMaybeTruncated = { tags: gh.tags.length === 100, branches: gh.branches.length === 100 };

  // First contact with a resolvable repo: suggest the ref the contributing
  // guide would suggest: the latest tag when one exists, else the default
  // branch. Only while the user hasn't taken the wheel; a suggestion made
  // for a previous repository is re-made, never kept.
  if (!state.ui.refTouched && (!state.form.refValue.trim() || state.ui.refAutofilled)) {
    if (gh.tags.length) {
      state.form.refKind = "tag";
      state.form.refValue = gh.tags[0];
    } else {
      state.form.refKind = "branch";
      state.form.refValue = gh.info.default_branch;
    }
    state.ui.refAutofilled = true;
    state.ui.refCustom = false;
    state.github.refCheck = { key: "", status: "idle" };
    syncRefInputs();
  }
  updateRefDatalist();
  renderRefControl();
  scheduleRefCheck();
  scheduleScan();
  refresh();
}

async function checkRefUpstream() {
  const gh = state.github;
  const f = state.form;
  const value = f.refValue.trim();
  if (!gh.slug || gh.status !== "ok" || !value || f.refKind === "sha") return;
  const key = refCheckKey();
  if (gh.refCheck.key === key && gh.refCheck.status !== "idle") return;

  const list = f.refKind === "tag" ? gh.tags : gh.branches;
  const maybeTruncated =
    f.refKind === "tag" ? gh.listsMaybeTruncated.tags : gh.listsMaybeTruncated.branches;
  if (list.includes(value)) {
    gh.refCheck = { key, status: "ok" };
    refresh();
    return;
  }
  if (!maybeTruncated) {
    gh.refCheck = { key, status: "missing" };
    refresh();
    return;
  }
  // The first 100 didn't contain it and there may be more: ask directly.
  gh.refCheck = { key, status: "checking" };
  refresh();
  const namespace = f.refKind === "branch" ? "heads" : "tags";
  const res = await ghJson(
    `/repos/${gh.slug.owner}/${gh.slug.repo}/git/ref/${namespace}/${encodeURIComponent(value)}`,
  );
  if (refCheckKey() !== key) return;
  if (res.rateLimited) {
    state.github.status = "ratelimited";
  } else {
    gh.refCheck = { key, status: res.ok ? "ok" : "missing" };
  }
  refresh();
}

// --- Maintainer email -> GitHub handle -----------------------------------
//
// package.xml carries name + email but no GitHub handle. Three tiers, in
// cost order: (1) users.noreply.github.com addresses encode the login
// directly; (2) the repo's commits filtered by author email (GitHub links
// commit emails to accounts, and maintainers usually have commits in their
// own repo); (3) a public-profile email search, accepted only on an exact
// single hit. (The global /search/commits endpoint would cover maintainers
// whose linked commits live elsewhere, but it sends no CORS headers, so
// browsers cannot call it.) Resolutions and definitive misses are cached
// per email; transient failures are not.

const handleCache = new Map(); // email (lowercased) -> login | null

function githubNoreplyLogin(email) {
  const m = email.match(
    /^(?:\d+\+)?([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})@users\.noreply\.github\.com$/i,
  );
  return m ? m[1] : null;
}

async function resolveGithubHandle(slug, email) {
  const key = email.toLowerCase();
  if (handleCache.has(key)) return handleCache.get(key);
  const noreply = githubNoreplyLogin(email);
  if (noreply) {
    handleCache.set(key, noreply);
    return noreply;
  }
  let login = null;
  const commits = await ghJson(
    `/repos/${slug.owner}/${slug.repo}/commits?author=${encodeURIComponent(email)}&per_page=1`,
  );
  if (commits.rateLimited || commits.status === 0) return null; // transient; don't cache
  if (commits.ok && Array.isArray(commits.data) && commits.data[0]?.author?.login) {
    login = commits.data[0].author.login;
  }
  if (!login) {
    const users = await ghJson(`/search/users?q=${encodeURIComponent(`${email} in:email`)}`);
    if (users.rateLimited || users.status === 0) return null;
    if (users.ok && users.data?.total_count === 1 && users.data.items?.[0]?.login) {
      login = users.data.items[0].login;
    }
  }
  handleCache.set(key, login);
  return login;
}

// Fire-and-forget after a scan renders: fills handles into the suggestion
// cards, and backfills any already-added maintainer row whose email matches
// and whose handle the user hasn't typed yet.
async function resolveHandles(key, harvested) {
  const gh = state.github;
  const slug = gh.slug;
  if (!slug) return;
  const targets = harvested.filter((m) => m.email && !m.github).slice(0, MAX_HANDLE_LOOKUPS);
  if (!targets.length) return;
  await Promise.all(
    targets.map(async (m) => {
      const login = await resolveGithubHandle(slug, m.email);
      if (login) m.github = login;
    }),
  );
  if (gh.scan.key !== key || gh.scan.status !== "done") return;
  let backfilled = false;
  for (const t of targets) {
    if (!t.github) continue;
    const row = state.form.maintainers.find(
      (m) => m.email.trim().toLowerCase() === t.email.toLowerCase() && !m.github.trim(),
    );
    if (row) {
      row.github = t.github;
      backfilled = true;
    }
  }
  if (backfilled) renderMaintainers();
  refresh();
}

function parsePackageXml(text, path) {
  let doc;
  try {
    doc = new DOMParser().parseFromString(text, "text/xml");
  } catch {
    return null;
  }
  if (doc.querySelector("parsererror")) return null;
  const root = doc.querySelector("package");
  if (!root) return null;
  const name = root.querySelector(":scope > name")?.textContent?.trim() || "";
  if (!name) return null;
  const description = (root.querySelector(":scope > description")?.textContent || "")
    .replace(/\s+/g, " ")
    .trim();
  const maintainers = [...root.querySelectorAll(":scope > maintainer")].map((m) => ({
    name: (m.textContent || "").replace(/\s+/g, " ").trim(),
    email: (m.getAttribute("email") || "").trim(),
    github: "",
  }));
  return { name, description, path, maintainers };
}

async function scanPackages(force = false) {
  const gh = state.github;
  const f = state.form;
  const value = f.refValue.trim();
  if (!gh.slug || gh.status !== "ok" || !value) return;
  const key = scanKey();
  // A previous error (e.g. a rate-limited tree fetch) never blocks a retry.
  if (!force && gh.scan.key === key && gh.scan.status !== "idle" && gh.scan.status !== "error")
    return;

  gh.scan = { key, status: "scanning", found: [], maintainers: [], note: "" };
  refresh();

  const tree = await ghJson(
    `/repos/${gh.slug.owner}/${gh.slug.repo}/git/trees/${encodeURIComponent(value)}?recursive=1`,
  );
  if (scanKey() !== key) return;
  if (tree.rateLimited) {
    gh.status = "ratelimited";
    gh.scan = { key, status: "error", found: [], maintainers: [], note: "" };
    refresh();
    return;
  }
  if (!tree.ok || !Array.isArray(tree.data?.tree)) {
    gh.scan = {
      key,
      status: "error",
      found: [],
      maintainers: [],
      note: "couldn't read the tree at this ref",
    };
    refresh();
    return;
  }

  const paths = tree.data.tree
    .filter((n) => n.type === "blob" && /(^|\/)package\.xml$/.test(n.path))
    .map((n) => n.path)
    .sort((a, b) => a.split("/").length - b.split("/").length || a.localeCompare(b));
  const capped = paths.slice(0, MAX_SCAN_FILES);
  let note = "";
  if (tree.data.truncated) note = "large repository; the file listing was truncated by GitHub";
  else if (paths.length > MAX_SCAN_FILES)
    note = `showing the first ${MAX_SCAN_FILES} of ${paths.length} package.xml files`;

  const texts = await Promise.all(
    capped.map(async (p) => {
      try {
        const res = await fetch(
          `https://raw.githubusercontent.com/${gh.slug.owner}/${gh.slug.repo}/${encodeURIComponent(value)}/${p.split("/").map(encodeURIComponent).join("/")}`,
        );
        return res.ok ? await res.text() : null;
      } catch {
        return null;
      }
    }),
  );
  if (scanKey() !== key) return;

  const found = [];
  for (let i = 0; i < capped.length; i++) {
    if (!texts[i]) continue;
    const parsed = parsePackageXml(texts[i], capped[i]);
    if (parsed) found.push(parsed);
  }
  found.sort((a, b) => a.name.localeCompare(b.name));

  const seen = new Set();
  const harvested = [];
  for (const pkg of found) {
    for (const m of pkg.maintainers) {
      const id = m.email.toLowerCase() || m.name.toLowerCase();
      if (!id || seen.has(id)) continue;
      seen.add(id);
      harvested.push(m);
    }
  }

  gh.scan = { key, status: "done", found, maintainers: harvested, note };
  renderFoundPackages();
  renderMaintainerSuggestions();
  refresh();
  resolveHandles(key, harvested); // async; re-renders suggestions as handles land
}

const scheduleInspect = debounce(inspectRepo, 500);
const scheduleRefCheck = debounce(checkRefUpstream, 400);
const scheduleScan = debounce(() => scanPackages(false), 700);

// --- Rendering: buffer ---------------------------------------------------------

function tokenizeLine(line) {
  const span = el("span", { class: "tok-line" });
  const comment = line.match(/^(\s*)(#.*)$/);
  if (comment) {
    span.append(comment[1], el("span", { class: "tok-comment", text: comment[2] }));
    return span;
  }
  const kv = line.match(/^(\s*)(- )?([\p{L}0-9_<> -]+?):($|\s.*$|.*$)/u);
  if (kv) {
    const [, indent, dash, key, restRaw] = kv;
    span.append(indent);
    if (dash) span.append(el("span", { class: "tok-punct", text: dash }));
    span.append(el("span", { class: "tok-key", text: key }));
    span.append(el("span", { class: "tok-punct", text: ":" }));
    const rest = restRaw;
    if (rest) {
      const trimmed = rest.trimStart();
      const pad = rest.slice(0, rest.length - trimmed.length);
      span.append(pad);
      if (trimmed.startsWith("#")) span.append(el("span", { class: "tok-comment", text: trimmed }));
      else if (trimmed.startsWith('"'))
        span.append(el("span", { class: "tok-str", text: trimmed }));
      else {
        const hint = trimmed.indexOf(" #");
        if (hint >= 0) {
          span.append(trimmed.slice(0, hint));
          span.append(el("span", { class: "tok-comment", text: trimmed.slice(hint) }));
        } else {
          span.append(trimmed);
        }
      }
    }
    return span;
  }
  const item = line.match(/^(\s*)- (.*)$/);
  if (item) {
    span.append(item[1], el("span", { class: "tok-punct", text: "- " }));
    if (item[2].startsWith('"')) span.append(el("span", { class: "tok-str", text: item[2] }));
    else span.append(item[2]);
    return span;
  }
  span.append(line);
  return span;
}

function renderBuffer() {
  const lines = entryLines();
  const code = document.getElementById("buffer-code");
  code.textContent = "";
  const prev = state.ui.lastLines;
  lines.forEach((line, i) => {
    const node = tokenizeLine(line);
    if (prev.length && line !== prev[i]) node.classList.add("tok-line--flash");
    code.append(node);
  });
  // An editor has a cursor; the entry is being written.
  const last = code.lastElementChild;
  if (last) last.append(el("span", { class: "tok-caret", "aria-hidden": "true" }));
  state.ui.lastLines = lines;
  state.ui.entryText = lines.join("\n") + "\n";
}

// --- Rendering: gates + handoff -------------------------------------------------

const GATE_ICON = { ok: "✓", fail: "✕", warn: "!", pending: "", info: "i" };

function renderGates(result) {
  const list = document.getElementById("gates");
  list.textContent = "";
  for (const gate of result.gates) {
    list.append(
      el(
        "li",
        { class: "gate", "data-status": gate.status },
        el("span", {
          class: "gate-icon",
          text: GATE_ICON[gate.status] || "",
          "aria-hidden": "true",
        }),
        el(
          "div",
          {},
          el("span", { class: "gate-name", text: gate.name }),
          el("p", { class: "gate-msg", text: `${statusWord(gate.status)}: ${gate.msg}` }),
        ),
      ),
    );
  }
}

function statusWord(status) {
  return (
    { ok: "pass", fail: "fail", warn: "warning", pending: "pending", info: "note" }[status] ||
    status
  );
}

// Deep link to the register-request issue form with every field pre-filled
// (params are the form's field ids; the required DCO checkbox cannot be
// pre-ticked by design). Submitting it makes the register workflow open the
// PR: no fork, no paste, no indentation to get wrong.
function registrationIssueUrl() {
  const params = new URLSearchParams({
    template: "register-request.yml",
    title: prTitle(),
    distro: state.distro,
    entry: state.ui.entryText || "",
  });
  return `https://github.com/${REGISTRY_REPO}/issues/new?${params.toString()}`;
}

function renderHandoff(result) {
  const handoff = document.getElementById("handoff");
  handoff.dataset.ready = result.ready ? "true" : "false";
  // inert removes the dimmed content from keyboard/AT interaction too; the
  // CSS pointer-events rule alone only locks out mouse users.
  const body = handoff.querySelector(".handoff-body");
  if (body) body.inert = !result.ready;
  document.getElementById("handoff-request").setAttribute("href", registrationIssueUrl());
  let blocked = handoff.querySelector(".handoff-blocked");
  if (!result.ready) {
    const remaining = result.gates.filter(
      (g) => g.status === "fail" || g.status === "pending",
    ).length;
    if (!blocked) {
      blocked = el("p", { class: "handoff-blocked" });
      handoff.querySelector("h2").after(blocked);
    }
    blocked.textContent = `Unlocks when the pre-flight is green: ${remaining} check(s) to go.`;
  } else if (blocked) {
    blocked.remove();
  }
  document.getElementById("pr-title").textContent = prTitle();
}

function prTitle() {
  const key = state.form.repoName.trim() || "<entry-name>";
  return `feat(${state.distro}): register ${key}`;
}

// --- Rendering: station 1 statuses ----------------------------------------------

function renderUrlStatus() {
  const node = document.getElementById("url-status");
  const gh = state.github;
  const f = state.form;
  const registered = existingFor(state.distro);
  const canon = f.url.trim() ? canonicalUrl(f.url) : "";

  if (!f.url.trim()) {
    setStatus(node, "", "");
    delete node.dataset.problemKey;
    return;
  }
  // Malformed / SSH remotes take precedence over everything else; when an
  // https equivalent can be derived, offer it as a one-click fix. Built
  // manually (not via setStatus) because of the button; the problemKey
  // guard keeps the aria-live region quiet while the problem is unchanged.
  const problem = gitUrlProblem(f.url);
  if (problem) {
    const key = `${f.url.trim()}|${problem.msg}`;
    if (node.dataset.problemKey !== key) {
      node.dataset.problemKey = key;
      node.dataset.tone = "bad";
      node.textContent = problem.msg;
      if (problem.fix) {
        const btn = el("button", {
          type: "button",
          class: "found-add",
          text: `Use ${problem.fix}`,
        });
        btn.addEventListener("click", () => {
          const input = document.getElementById("repo-url");
          input.value = problem.fix;
          input.dispatchEvent(new Event("input", { bubbles: true }));
        });
        node.append(" ", btn);
      }
    }
    return;
  }
  delete node.dataset.problemKey;
  if (canon && registered.urls.has(canon)) {
    setStatus(
      node,
      `Already registered in ${state.distro} as “${registered.urls.get(canon)}”; update that entry instead of adding a new one.`,
      "bad",
    );
    return;
  }
  switch (gh.status) {
    case "loading":
      setStatus(node, "Looking up the repository on GitHub…", "");
      break;
    case "ok": {
      const info = gh.info;
      if (!gh.slug || !info) {
        setStatus(node, "", "");
        break;
      }
      const bits = [
        `Found ${gh.slug.owner}/${gh.slug.repo}`,
        `default branch ${info.default_branch}`,
        `${gh.tags.length}${gh.listsMaybeTruncated.tags ? "+" : ""} tag(s)`,
      ];
      if (info.archived) bits.push("⚠ archived");
      setStatus(node, bits.join(" · "), info.archived ? "warn" : "ok");
      break;
    }
    case "notfound":
      setStatus(
        node,
        "Not found on GitHub; check the URL (private repositories can't be swept).",
        "bad",
      );
      break;
    case "ratelimited": {
      const rl = rateLimitInfo();
      const detail = rl
        ? ` (0/${rl.limit} requests left this hour for your IP; resets at ${rl.hhmm}, in ~${rl.mins} min)`
        : "";
      setStatus(
        node,
        `GitHub API rate limit reached${detail}. Auto-discovery resumes automatically; manual entry works meanwhile and CI validates everything.`,
        "warn",
      );
      break;
    }
    case "pageurl":
      setStatus(
        node,
        "This looks like a GitHub page URL; register the repository clone URL (without /tree/…).",
        "bad",
      );
      break;
    case "incomplete":
      setStatus(node, "This github.com URL is missing its /owner/repository path.", "bad");
      break;
    case "nonGithub":
      setStatus(
        node,
        "Not a github.com URL: auto-discovery is off; CI still verifies the ref with git ls-remote.",
        "",
      );
      break;
    case "error":
      setStatus(node, "GitHub API error; try again, or continue manually.", "warn");
      break;
    default:
      setStatus(node, "", "");
  }
}

function renderRepoNameStatus() {
  const node = document.getElementById("repo-name-status");
  const input = document.getElementById("repo-name");
  const key = state.form.repoName.trim();
  const registered = existingFor(state.distro);
  if (!key) {
    setStatus(node, "", "");
    input.removeAttribute("aria-invalid");
    return;
  }
  if (!REPO_KEY_RE.test(key)) {
    setStatus(node, "Lowercase letters, digits, _ or - (it must start with a letter).", "bad");
    input.setAttribute("aria-invalid", "true");
  } else if (registered.repoNames.has(key)) {
    setStatus(node, `“${key}” is already an entry in ${state.distro}; pick another name.`, "bad");
    input.setAttribute("aria-invalid", "true");
  } else {
    setStatus(node, "", "");
    input.removeAttribute("aria-invalid");
  }
}

function setStatus(node, text, tone) {
  // No-op when nothing changed: these are aria-live regions, and rewriting
  // identical text on every keystroke makes screen readers re-announce it.
  if (node.textContent === text && (node.dataset.tone || "") === (tone || "")) return;
  node.textContent = text;
  if (tone) node.dataset.tone = tone;
  else delete node.dataset.tone;
}

// Rebuilding a zone that contains the focused button would silently drop
// keyboard focus to <body>. Key every rebuilt button with data-fkey and
// restore focus to its replacement (or the nearest enabled neighbour).
function withFocusRestore(container, rebuild) {
  const active = document.activeElement;
  const key = active && container.contains(active) ? active.dataset.fkey : null;
  rebuild();
  if (!key) return;
  const next = container.querySelector(`[data-fkey="${CSS.escape(key)}"]`);
  if (next && !next.disabled) next.focus();
  else {
    const alt = container.querySelector("button:not([disabled])");
    if (alt) alt.focus();
  }
}

// --- Rendering: station 2 --------------------------------------------------------

const REF_LABELS = { tag: "Tag", branch: "Branch", sha: "Commit SHA" };
const REF_PLACEHOLDERS = { tag: "1.2.0", branch: "main", sha: "40-character commit sha" };

function syncRefInputs() {
  const f = state.form;
  document.querySelectorAll('input[name="refkind"]').forEach((r) => {
    r.checked = r.value === f.refKind;
  });
  const input = document.getElementById("ref-value");
  input.value = f.refValue;
  document.getElementById("ref-value-label").textContent = REF_LABELS[f.refKind];
  input.placeholder = REF_PLACEHOLDERS[f.refKind];
}

function refListValues() {
  const gh = state.github;
  if (state.form.refKind === "sha" || gh.status !== "ok") return [];
  return state.form.refKind === "branch" ? gh.branches : gh.tags;
}

// The ref value is a real dropdown of the fetched branches/tags whenever the
// list is available, with an "Other: type a name…" escape hatch (needed for
// refs beyond the first 100 anyway). No list (sha kind, non-GitHub host,
// rate limit, API error) falls back to the free-text input. The swap never
// happens under the user's cursor: while the text input is focused it stays.
function renderRefControl() {
  const f = state.form;
  const select = document.getElementById("ref-select");
  const input = document.getElementById("ref-value");
  const values = refListValues();
  const value = f.refValue.trim();
  const typing = document.activeElement === input;
  const useSelect =
    values.length > 0 && !state.ui.refCustom && !typing && (!value || values.includes(value));
  select.hidden = !useSelect;
  input.hidden = useSelect;
  if (!useSelect) return;
  select.textContent = "";
  if (!value) {
    select.append(
      el("option", {
        value: "",
        text: `Select a ${f.refKind}…`,
        disabled: "",
        selected: "",
        hidden: "",
      }),
    );
  }
  for (const v of values) select.append(el("option", { value: v, text: v }));
  select.append(el("option", { value: CUSTOM_REF, text: `Other: type a ${f.refKind} name…` }));
  select.value = value;
}

function updateRefDatalist() {
  const datalist = document.getElementById("ref-options");
  datalist.textContent = "";
  const gh = state.github;
  if (gh.status !== "ok") return;
  const values = state.form.refKind === "branch" ? gh.branches : gh.tags;
  if (state.form.refKind === "sha") return;
  for (const v of values.slice(0, 50)) datalist.append(el("option", { value: v }));
}

function renderRefStatus() {
  const node = document.getElementById("ref-status");
  const extra = document.getElementById("ref-extra");
  extra.textContent = "";
  const f = state.form;
  const gh = state.github;
  const value = f.refValue.trim();

  if (f.refKind === "sha") {
    if (gh.slug && gh.status === "ok") {
      const btn = el("button", {
        type: "button",
        class: "btn-secondary",
        text: `Use the latest commit on ${gh.info.default_branch}`,
      });
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        const slugKey = `${gh.slug.owner}/${gh.slug.repo}`;
        const res = await ghJson(
          `/repos/${slugKey}/commits?per_page=1&sha=${encodeURIComponent(gh.info.default_branch)}`,
        );
        btn.disabled = false;
        // The URL may have changed while the request was in flight; never
        // pin the new repository at the old repository's commit.
        const current = state.github.slug;
        if (!current || `${current.owner}/${current.repo}` !== slugKey) return;
        if (res.ok && Array.isArray(res.data) && res.data[0]?.sha) {
          state.form.refValue = res.data[0].sha;
          state.ui.refTouched = true;
          syncRefInputs();
          onRefChanged();
        } else if (res.rateLimited) {
          state.github.status = "ratelimited";
          refresh();
        }
      });
      extra.append(btn);
    }
    if (!value) setStatus(node, "", "");
    else if (SHA_RE.test(value)) setStatus(node, "Valid sha format.", "ok");
    else setStatus(node, "A sha ref is exactly 40 lowercase hex characters.", "bad");
    return;
  }

  // In custom-entry mode, offer the way back to the dropdown.
  if (state.ui.refCustom && refListValues().length) {
    const back = el("button", {
      type: "button",
      class: "ref-toggle",
      text: `Choose from the ${f.refKind} list instead`,
    });
    back.addEventListener("click", () => {
      state.ui.refCustom = false;
      const values = refListValues();
      if (f.refValue.trim() && !values.includes(f.refValue.trim())) {
        state.form.refValue = "";
        syncRefInputs();
      }
      renderRefControl();
      document.getElementById("ref-select").focus();
      onRefChanged();
    });
    extra.append(back);
  }

  if (f.refKind === "tag" && gh.status === "ok" && !gh.tags.length) {
    setStatus(
      node,
      "This repository has no tags yet; register the default branch instead, and switch to a tag once you cut a release.",
      "warn",
    );
    return;
  }
  if (!value || gh.status !== "ok") {
    setStatus(node, "", "");
    return;
  }
  const check = gh.refCheck;
  if (check.key !== refCheckKey() || check.status === "checking")
    setStatus(node, "Checking upstream…", "");
  else if (check.status === "ok")
    setStatus(node, `${REF_LABELS[f.refKind]} exists upstream.`, "ok");
  else if (check.status === "missing")
    setStatus(node, `No ${f.refKind} named “${value}” upstream.`, "bad");
  else setStatus(node, "", "");
}

// --- Rendering: station 3 (packages) ---------------------------------------------

const pkgNodes = new Map(); // pkg.id -> node refs for cheap updates

function addPackage(prefill = {}) {
  const pkg = {
    id: `p${++pkgSeq}`,
    name: prefill.name || "",
    tags: [],
    description: "",
    maintainers: [],
    upstream: prefill.upstream || null,
  };
  state.form.packages.push(pkg);
  renderPackages();
  refresh();
  return pkg;
}

function removePackage(id) {
  state.form.packages = state.form.packages.filter((p) => p.id !== id);
  renderPackages();
  refresh();
}

function tagPicker(pkg) {
  const wrap = el("div", {});
  const groups = state.vocabulary.groups.length
    ? state.vocabulary.groups
    : [{ id: "", title: "Tags" }];
  const chipRefs = new Map();
  for (const group of groups) {
    const tags = state.vocabulary.tags.filter((t) => (t.group || "") === (group.id || ""));
    if (!tags.length) continue;
    const chips = el("div", { class: "tagpick-chips" });
    for (const tag of tags) {
      let title = tag.disambiguation ? `${tag.summary} ${tag.disambiguation}` : tag.summary;
      // A labeled chip shows the label but stores the id, so the tooltip
      // names the id the generated YAML will carry.
      if (tag.label && tag.label !== tag.id) title = `Stored as \`${tag.id}\`. ${title}`;
      const chip = el("button", {
        type: "button",
        class: "tagchip",
        text: tag.label || tag.id,
        title,
        "aria-pressed": "false",
      });
      chip.addEventListener("click", () => {
        const i = pkg.tags.indexOf(tag.id);
        if (i >= 0) pkg.tags.splice(i, 1);
        else pkg.tags.push(tag.id);
        chip.setAttribute("aria-pressed", pkg.tags.includes(tag.id) ? "true" : "false");
        refresh();
      });
      chipRefs.set(tag.id, chip);
      chips.append(chip);
    }
    wrap.append(
      el(
        "div",
        { class: "tagpick-group" },
        el("p", { class: "tagpick-group-title", text: group.title }),
        chips,
      ),
    );
  }
  const meta = el("p", { class: "tagpick-meta" });
  wrap.append(meta);
  return { wrap, chipRefs, meta };
}

function packageCard(pkg) {
  const nameInput = el("input", {
    type: "text",
    class: "mono pkg-name",
    value: pkg.name,
    placeholder: "package_name (must equal the package.xml <name>)",
    autocomplete: "off",
    spellcheck: "false",
    "aria-label": "Package name",
  });
  nameInput.addEventListener("input", () => {
    pkg.name = nameInput.value;
    refresh();
  });
  const removeBtn = el("button", {
    type: "button",
    class: "pkg-remove",
    text: "Remove",
    "aria-label": `Remove ${pkg.name || "this package"} from the entry`,
  });
  removeBtn.addEventListener("click", () => removePackage(pkg.id));

  const nameStatus = el("p", { class: "status", id: `${pkg.id}-name-status` });

  const picker = tagPicker(pkg);
  picker.wrap.setAttribute("role", "group");
  picker.wrap.setAttribute("aria-labelledby", `${pkg.id}-tags-label`);

  const desc = el("textarea", {
    id: `${pkg.id}-desc`,
    rows: "2",
    placeholder: "Optional; overrides the package.xml description on the browse card.",
  });
  desc.value = pkg.description;
  desc.addEventListener("input", () => {
    pkg.description = desc.value;
    refresh();
  });
  const descHint = el("p", { class: "hint" });
  if (pkg.upstream?.description) {
    descHint.textContent = `Upstream package.xml says: “${pkg.upstream.description}”; leave empty to use it.`;
  } else {
    descHint.textContent =
      "Leave empty to show the description the sweep caches from the upstream package.xml.";
  }

  const overrideRows = el("div", {});
  const overrideAdd = el("button", {
    type: "button",
    class: "btn-secondary",
    text: "+ Add an override maintainer",
  });
  overrideAdd.addEventListener("click", () => {
    pkg.maintainers.push(blankMaintainer());
    renderMaintainerRows(overrideRows, pkg.maintainers);
    refresh();
  });
  renderMaintainerRows(overrideRows, pkg.maintainers);

  const root = el(
    "div",
    { class: "pkg", "data-id": pkg.id },
    el("div", { class: "pkg-head" }, nameInput, removeBtn),
    nameStatus,
    el(
      "div",
      { class: "field" },
      el("p", {
        class: "field-label",
        id: `${pkg.id}-tags-label`,
        text: "Tags: the first one is the package's primary identity",
      }),
      picker.wrap,
    ),
    el(
      "div",
      { class: "field" },
      el("label", { for: `${pkg.id}-desc`, text: "Description override" }),
      desc,
      descHint,
    ),
    el(
      "details",
      { class: "pkg-advanced" },
      el("summary", {
        text: "Maintainers differ for this package? Add an override",
      }),
      el("p", { class: "hint", text: "Leave empty to inherit the repository maintainers below." }),
      overrideRows,
      overrideAdd,
    ),
  );
  pkgNodes.set(pkg.id, { root, nameInput, nameStatus, picker });
  return root;
}

function renderPackages() {
  const list = document.getElementById("pkg-list");
  list.textContent = "";
  pkgNodes.clear();
  for (const pkg of state.form.packages) list.append(packageCard(pkg));
}

function updatePackageCards() {
  const registered = existingFor(state.distro);
  const counts = new Map();
  for (const p of state.form.packages) {
    const n = p.name.trim();
    if (n) counts.set(n, (counts.get(n) || 0) + 1);
  }
  for (const pkg of state.form.packages) {
    const nodes = pkgNodes.get(pkg.id);
    if (!nodes) continue;
    const name = pkg.name.trim();
    let msg = "";
    let tone = "";
    if (name && !PKG_NAME_RE.test(name)) {
      msg = "Lowercase letters, digits and _ only (ROS package names have no hyphens).";
      tone = "bad";
    } else if (name && registered.packages.has(name)) {
      msg = `Already registered in ${state.distro} by “${registered.packages.get(name)}”.`;
      tone = "bad";
    } else if (name && counts.get(name) > 1) {
      msg = "Listed twice in this entry.";
      tone = "bad";
    } else if (
      name &&
      state.github.scan.status === "done" &&
      state.github.scan.key === scanKey() && // stale scans prove nothing
      state.github.scan.found.length &&
      !state.github.scan.note && // a truncated scan can't prove absence
      !state.github.scan.found.some((f) => f.name === name)
    ) {
      msg = "Not among the package.xml names found at this ref; the sweep will fail on it.";
      tone = "warn";
    }
    setStatus(nodes.nameStatus, msg, tone);
    if (msg) nodes.nameInput.setAttribute("aria-describedby", nodes.nameStatus.id);
    else nodes.nameInput.removeAttribute("aria-describedby");
    if (tone === "bad") nodes.nameInput.setAttribute("aria-invalid", "true");
    else nodes.nameInput.removeAttribute("aria-invalid");

    // Tag chips: pressed state.
    for (const [id, chip] of nodes.picker.chipRefs) {
      const selected = pkg.tags.includes(id);
      chip.setAttribute("aria-pressed", selected ? "true" : "false");
    }
    nodes.picker.meta.textContent = "";
    nodes.picker.meta.append(`${pkg.tags.length} selected`);
    if (pkg.tags.length) {
      nodes.picker.meta.append(" · order: ");
      nodes.picker.meta.append(el("b", { text: pkg.tags.join(" → ") }));
    }
  }
}

function renderFoundPackages() {
  const zone = document.getElementById("scan-zone");
  const status = document.getElementById("scan-status");
  const list = document.getElementById("found-list");
  const scan = state.github.scan;
  const show = state.github.slug && state.github.status === "ok";
  zone.hidden = !show;
  if (!show) return;
  zone.hidden = scan.status === "idle";
  withFocusRestore(zone, () => renderFoundPackagesInto(status, list, scan));
}

function renderFoundPackagesInto(status, list, scan) {
  list.textContent = "";
  if (scan.status === "scanning") {
    status.textContent = `Scanning ${state.github.slug.owner}/${state.github.slug.repo} at “${state.form.refValue.trim()}” for package.xml files…`;
    return;
  }
  if (scan.status === "error") {
    status.textContent = `Couldn't scan the repository${scan.note ? ` (${scan.note})` : ""}. Add packages manually below.`;
    return;
  }
  if (scan.status !== "done") {
    status.textContent = "";
    return;
  }
  // Results from a previous ref are stale, not wrong; say so while the
  // debounced rescan is on its way.
  if (scan.key !== scanKey()) {
    status.textContent = "Ref changed, rescanning…";
    return;
  }

  const added = new Set(state.form.packages.map((p) => p.name.trim()));
  const registered = existingFor(state.distro);

  status.textContent = "";
  if (!scan.found.length) {
    status.append(`No package.xml found at this ref. `);
  } else {
    status.append(`Found ${scan.found.length} package(s) at this ref`);
    if (scan.note) status.append(` (${scan.note})`);
    status.append(". ");
    const addable = scan.found.filter(
      (f) => !added.has(f.name) && !registered.packages.has(f.name),
    );
    if (addable.length > 1) {
      const all = el("button", {
        type: "button",
        class: "found-add",
        text: `Add all ${addable.length}`,
        "data-fkey": "add-all",
      });
      all.addEventListener("click", () => {
        for (const f of addable)
          addPackage({ name: f.name, upstream: { description: f.description, path: f.path } });
      });
      status.append(all);
    }
  }
  const rescan = el("button", {
    type: "button",
    class: "found-add",
    text: "Rescan",
    "data-fkey": "rescan",
  });
  rescan.addEventListener("click", () => scanPackages(true));
  status.append(" ", rescan);

  for (const f of scan.found) {
    const taken = registered.packages.has(f.name);
    const btn = el("button", {
      type: "button",
      class: "found-add",
      text: added.has(f.name) ? "Added ✓" : taken ? "Already registered" : "Add",
      "data-fkey": `add:${f.name}`,
      "aria-label": added.has(f.name)
        ? `${f.name} added`
        : taken
          ? `${f.name} is already registered`
          : `Add ${f.name}`,
    });
    btn.disabled = added.has(f.name) || taken;
    btn.addEventListener("click", () => {
      addPackage({ name: f.name, upstream: { description: f.description, path: f.path } });
    });
    list.append(
      el(
        "div",
        { class: "found" },
        el(
          "div",
          { class: "found-text" },
          el("span", { class: "found-name", text: f.name }),
          el("span", { class: "found-desc", text: f.description ? ` · ${f.description}` : "" }),
          el("br"),
          el("span", { class: "found-desc", text: f.path }),
        ),
        btn,
      ),
    );
  }
}

// --- Rendering: station 4 (maintainers) -------------------------------------------

let maintRowSeq = 0;

function maintainerRow(m, list) {
  const errorsId = `maint-errors-${++maintRowSeq}`;
  const avatar = el("img", { class: "maint-avatar", alt: "", hidden: "" });
  const slot = el("span", { class: "maint-avatar-slot", "aria-hidden": "true" });
  const nameIn = el("input", {
    type: "text",
    value: m.name,
    placeholder: "Full name",
    "aria-label": "Maintainer name",
    autocomplete: "off",
  });
  const emailIn = el("input", {
    type: "email",
    value: m.email,
    placeholder: "email@org.dev",
    "aria-label": "Maintainer email",
    autocomplete: "off",
    spellcheck: "false",
  });
  const ghIn = el("input", {
    type: "text",
    class: "mono",
    value: m.github,
    placeholder: "github-handle",
    "aria-label": "Maintainer GitHub handle",
    autocomplete: "off",
    spellcheck: "false",
  });
  const errors = el("p", { class: "maint-errors", id: errorsId });
  const remove = el("button", {
    type: "button",
    class: "maint-remove",
    text: "Remove",
    "aria-label": `Remove maintainer ${m.name || ""}`.trim(),
  });

  const syncAvatar = () => {
    const handle = m.github.trim();
    if (handle && !PLACEHOLDER_NAMES.has(handle.toLowerCase())) {
      // github.com/<user>.png needs no API quota; onerror hides it again.
      avatar.src = `https://github.com/${encodeURIComponent(handle)}.png?size=64`;
      avatar.hidden = false;
      slot.hidden = true;
    } else {
      avatar.hidden = true;
      slot.hidden = false;
    }
  };
  avatar.addEventListener("error", () => {
    avatar.hidden = true;
    slot.hidden = false;
  });
  nameIn.addEventListener("input", () => {
    m.name = nameIn.value;
    refresh();
  });
  emailIn.addEventListener("input", () => {
    m.email = emailIn.value;
    refresh();
  });
  ghIn.addEventListener("input", () => {
    m.github = ghIn.value;
    refresh();
  });
  ghIn.addEventListener("blur", syncAvatar);
  syncAvatar();

  const row = el(
    "div",
    { class: "maint-row" },
    el("span", {}, avatar, slot),
    nameIn,
    emailIn,
    ghIn,
    remove,
    errors,
  );
  remove.addEventListener("click", () => {
    const i = list.indexOf(m);
    if (i >= 0) list.splice(i, 1);
    if (list === state.form.maintainers && !list.length) list.push(blankMaintainer());
    if (list === state.form.maintainers) renderMaintainers();
    else row.remove();
    refresh();
  });

  row.update = () => {
    const problems = placeholderProblems(m);
    if (m.email.trim() && !/^\S+@\S+\.\S+$/.test(m.email.trim())) {
      problems.push("this doesn't look like an email address");
    }
    errors.textContent = problems.join("; ");
    for (const [input, bad] of [
      [nameIn, problems.some((p) => p.includes("name"))],
      [emailIn, problems.some((p) => p.includes("email"))],
      [ghIn, problems.some((p) => p.includes("handle"))],
    ]) {
      if (bad) {
        input.setAttribute("aria-invalid", "true");
        input.setAttribute("aria-describedby", errorsId);
      } else {
        input.removeAttribute("aria-invalid");
        input.removeAttribute("aria-describedby");
      }
    }
  };
  return row;
}

function renderMaintainerRows(container, list) {
  container.textContent = "";
  for (const m of list) container.append(maintainerRow(m, list));
}

function renderMaintainers() {
  renderMaintainerRows(document.getElementById("maint-rows"), state.form.maintainers);
}

function updateMaintainerRows() {
  document.querySelectorAll(".maint-row").forEach((row) => row.update && row.update());
}

function renderMaintainerSuggestions() {
  const zone = document.getElementById("maint-suggestions");
  withFocusRestore(zone, () => renderMaintainerSuggestionsInto(zone));
}

function renderMaintainerSuggestionsInto(zone) {
  zone.textContent = "";
  const harvested = state.github.scan.maintainers;
  if (!harvested.length) return;
  // Same identity rule as the harvest (email, else name) so email-less
  // suggestions disappear once added instead of duplicating on every click.
  const have = new Set(
    state.form.maintainers.flatMap((m) =>
      [m.email.trim().toLowerCase(), m.name.trim().toLowerCase()].filter(Boolean),
    ),
  );
  for (const m of harvested) {
    const id = (m.email || m.name || "").toLowerCase();
    if (!id || have.has(id)) continue;
    const btn = el("button", {
      type: "button",
      class: "found-add",
      text: "Add",
      "data-fkey": `sug:${id}`,
      "aria-label": `Add maintainer ${m.name || m.email}`,
    });
    btn.addEventListener("click", () => {
      const blank = state.form.maintainers.find(maintainerEmpty);
      const target = blank || blankMaintainer();
      target.name = m.name;
      target.email = m.email;
      target.github = m.github || "";
      if (!blank) state.form.maintainers.push(target);
      renderMaintainers();
      renderMaintainerSuggestions();
      refresh();
    });
    const details = [
      m.email ? ` · ${m.email}` : "",
      m.github ? ` · @${m.github}` : "",
      " · from package.xml",
      m.github ? "" : " (add their GitHub handle)",
    ].join("");
    zone.append(
      el(
        "div",
        { class: "found" },
        el(
          "div",
          {},
          el("span", { class: "found-name", text: m.name || m.email }),
          el("span", { class: "found-desc", text: details }),
        ),
        btn,
      ),
    );
  }
}

// --- Refresh cycle -----------------------------------------------------------------

function refresh() {
  const result = validate();
  renderBuffer();
  renderGates(result);
  renderHandoff(result);
  renderUrlStatus();
  renderRepoNameStatus();
  renderRefStatus();
  renderFoundPackages(); // "Add" buttons track added/renamed packages
  updatePackageCards();
  updateMaintainerRows();
  renderMaintainerSuggestions();
}

// --- Copy helpers (same fallback path as app.js) --------------------------------------

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

// The primary handoff copy: the CURRENT distributions/<distro>.yaml from
// main (raw.githubusercontent is CORS-open and doesn't touch the API rate
// limit) with the entry appended. Pasting a whole file (select all, paste)
// survives web editors that re-indent pasted blocks; pasting a nested
// fragment into the middle of a YAML file does not. Falls back to the
// entry-only copy when the file can't be fetched.
async function copyUpdatedFile(statusEl) {
  const distro = state.distro;
  let base = null;
  try {
    const res = await fetch(
      `https://raw.githubusercontent.com/${REGISTRY_REPO}/main/distributions/${distro}.yaml`,
      { cache: "no-cache" },
    );
    if (res.ok) base = await res.text();
  } catch {
    base = null;
  }
  if (base === null || !base.includes("repositories:")) {
    await copyText(state.ui.entryText, statusEl, "Entry");
    statusEl.textContent +=
      ". Couldn't fetch the current file, so this is the entry alone; paste it at the end of repositories: and keep the indentation";
    return;
  }
  await copyText(
    base.replace(/\n*$/, "\n") + state.ui.entryText,
    statusEl,
    `Updated ${distro}.yaml`,
  );
}

async function copyText(text, statusEl, label) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      fallbackCopy(text);
    }
    statusEl.textContent = `${label} copied`;
  } catch {
    try {
      fallbackCopy(text);
      statusEl.textContent = `${label} copied`;
    } catch {
      statusEl.textContent = "Copy failed; select the text and copy manually";
    }
  }
}

// --- Wiring ---------------------------------------------------------------------------

function suggestRepoName() {
  const canon = canonicalUrl(state.form.url);
  const base = canon.split("/").filter(Boolean).pop() || "";
  let key = base.toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
  key = key.replace(/^[^a-z]+/, "").replace(/-+$/, "");
  state.form.repoName = key;
  document.getElementById("repo-name").value = key;
}

// Governance mirrors the hosting org: a repository under the
// autowarefoundation GitHub org is foundation-governed, everything else
// defaults to community. Re-derived on every URL change until the user
// picks governance themselves.
function suggestGovernance() {
  const slug = parseGithub(state.form.url);
  const suggested = slug && slug.owner === "autowarefoundation" ? "foundation" : "community";
  state.form.governance = suggested;
  document.querySelectorAll('input[name="governance"]').forEach((r) => {
    r.checked = r.value === suggested;
  });
}

function setDistro(distro) {
  state.distro = distro;
  const file = `distributions/${distro}.yaml`;
  document.getElementById("head-file").textContent = file;
  document.getElementById("buffer-file").textContent = file;
  document
    .getElementById("handoff-open")
    .setAttribute("href", `https://github.com/${REGISTRY_REPO}/edit/main/${file}`);
  refresh();
}

function onRefChanged() {
  state.github.refCheck = { key: "", status: "idle" };
  updateRefDatalist();
  renderRefControl();
  scheduleRefCheck();
  scheduleScan();
  refresh();
}

function wire() {
  const distroSelect = document.getElementById("distro-select");
  distroSelect.addEventListener("change", () => setDistro(distroSelect.value));

  const urlInput = document.getElementById("repo-url");
  urlInput.addEventListener("input", () => {
    state.form.url = urlInput.value;
    if (!state.ui.repoNameTouched) suggestRepoName();
    if (!state.ui.governanceTouched) suggestGovernance();
    scheduleInspect();
    refresh();
  });

  const nameInput = document.getElementById("repo-name");
  nameInput.addEventListener("input", () => {
    state.form.repoName = nameInput.value;
    // "Touched" means the user is keeping their own name here; an emptied
    // field returns to auto-suggest ownership, so the next URL edit fills it
    // again instead of staying blank forever.
    state.ui.repoNameTouched = nameInput.value.trim() !== "";
    refresh();
  });

  document.querySelectorAll('input[name="governance"]').forEach((radio) =>
    radio.addEventListener("change", () => {
      if (radio.checked) state.form.governance = radio.value;
      state.ui.governanceTouched = true;
      refresh();
    }),
  );

  document.querySelectorAll('input[name="refkind"]').forEach((radio) =>
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      state.form.refKind = radio.value;
      state.ui.refTouched = true;
      state.ui.refCustom = false;
      // A value from the previous kind (a tag name on the branch list, a
      // branch on the tag list) is never right; preselect the obvious
      // candidate from the new kind's list instead.
      const values = refListValues();
      if (values.length && !values.includes(state.form.refValue.trim())) {
        state.form.refValue =
          radio.value === "branch" ? state.github.info?.default_branch || "" : values[0];
      }
      syncRefInputs();
      onRefChanged();
    }),
  );

  const refInput = document.getElementById("ref-value");
  refInput.addEventListener("input", () => {
    state.form.refValue = refInput.value;
    // Same ownership rule as the entry name; explicit custom-entry mode
    // stays user-owned even while empty.
    state.ui.refTouched = refInput.value.trim() !== "" || state.ui.refCustom;
    onRefChanged();
  });
  // While focused the input never swaps out from under the cursor; converge
  // back to the dropdown (when the typed value is in the list) on blur.
  refInput.addEventListener("blur", () => renderRefControl());

  const refSelect = document.getElementById("ref-select");
  refSelect.addEventListener("change", () => {
    state.ui.refTouched = true;
    if (refSelect.value === CUSTOM_REF) {
      state.ui.refCustom = true;
      state.form.refValue = "";
      syncRefInputs();
      renderRefControl();
      document.getElementById("ref-value").focus();
      onRefChanged();
      return;
    }
    state.form.refValue = refSelect.value;
    syncRefInputs();
    onRefChanged();
  });

  document.getElementById("add-package").addEventListener("click", () => {
    const pkg = addPackage();
    const nodes = pkgNodes.get(pkg.id);
    if (nodes) nodes.nameInput.focus();
  });

  document.getElementById("add-maintainer").addEventListener("click", () => {
    state.form.maintainers.push(blankMaintainer());
    renderMaintainers();
    refresh();
  });

  // Copy feedback lands next to the button that was pressed: the buffer's
  // Copy reports under the buffer, the handoff buttons report inside the
  // handoff (they can be a full viewport apart in the stacked layout).
  // While rate-limited, tick the countdown once per interval and resume
  // auto-discovery on the first tick past the reset time (a fresh 403 just
  // re-arms the limit state with the new reset).
  setInterval(() => {
    const gh = state.github;
    if (gh.status !== "ratelimited") return;
    if (gh.rateLimit?.reset && Date.now() >= gh.rateLimit.reset * 1000) {
      gh.rateLimit = null;
      inspectRepo();
      return;
    }
    refresh();
  }, 30000);

  const copyStatus = document.getElementById("copy-status");
  const handoffStatus = document.getElementById("handoff-copy-status");
  document
    .getElementById("copy-entry")
    .addEventListener("click", () => copyText(state.ui.entryText, copyStatus, "Entry"));
  document
    .getElementById("handoff-copy")
    .addEventListener("click", () => copyUpdatedFile(handoffStatus));
  document
    .getElementById("handoff-copy-entry")
    .addEventListener("click", () => copyText(state.ui.entryText, handoffStatus, "Entry"));
  document
    .getElementById("copy-pr-title")
    .addEventListener("click", () => copyText(prTitle(), handoffStatus, "PR title"));
}

// --- Boot -----------------------------------------------------------------------------

async function main() {
  let data;
  try {
    const res = await fetch("data.json", { cache: "no-cache" });
    data = await res.json();
  } catch {
    document
      .querySelector(".reg-form")
      .prepend(
        el("p", { class: "empty", text: "Could not load data.json; serve the site over HTTP." }),
      );
    return;
  }

  const packages = data.packages || [];
  const derived = [...new Set(packages.map((p) => p.distro))];
  state.distros =
    (Array.isArray(data.distros) && data.distros.length && data.distros) ||
    (derived.length ? derived.sort() : ["jazzy"]);
  state.vocabulary = data.tag_vocabulary || { groups: [], tags: [] };
  for (const t of state.vocabulary.tags || []) state.tagInfo.set(t.id, t);

  for (const p of packages) {
    if (!state.existing.has(p.distro))
      state.existing.set(p.distro, { urls: new Map(), repoNames: new Set(), packages: new Map() });
    const idx = state.existing.get(p.distro);
    if (p.repository) idx.urls.set(canonicalUrl(p.repository), p.repo_name || p.repository);
    if (p.repo_name) idx.repoNames.add(p.repo_name);
    idx.packages.set(p.name, p.repo_name || p.repository || "another entry");
  }

  const distroSelect = document.getElementById("distro-select");
  for (const d of state.distros) distroSelect.append(el("option", { value: d, text: d }));
  const initial = state.distros.includes("jazzy") ? "jazzy" : state.distros[0];
  distroSelect.value = initial;

  wire();
  renderMaintainers();
  syncRefInputs();
  renderRefControl();
  setDistro(initial);
}

main();
