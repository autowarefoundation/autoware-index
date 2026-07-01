// aw-index-cli — canonical registry→`.repos` composition, as an ES module.
//
// This mirrors `src/aw_index_cli/compose.py` 1:1 so a JavaScript consumer (the
// autoware-index browse site's "repos builder") can reuse the SAME transform
// instead of forking a second, drifting implementation. The Python CLI remains
// the reference; `tests/test_conformance.py` runs both this module and the
// Python compose over shared fixtures and asserts byte-for-byte identical
// `.repos` output, so the two cannot silently diverge.
//
// Dependency-free on purpose: no npm, no bundler. It is importable directly in
// the browser via `<script type="module">` and by Node's built-in test runner.

/** Keep equal to `src/aw_index_cli/__init__.py` `__version__` (asserted by conformance). */
export const VERSION = "0.3.0";

/** Registry defaults, mirroring `registry.py` `DEFAULT_REPO`/`DEFAULT_REF`. */
export const DEFAULT_REPO = "autowarefoundation/autoware-index";
export const DEFAULT_REF = "main";

/** Raised when a distribution cannot be composed into `.repos` entries. */
export class ComposeError extends Error {
  constructor(message) {
    super(message);
    this.name = "ComposeError";
  }
}

// Sort by code point, matching Python's `sorted()` on ASCII registry keys/names.
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

const isMapping = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

function rejectUnknown(singular, plural, missing) {
  if (missing.length) {
    const names = missing
      .slice()
      .sort(cmp)
      .map((n) => `'${n}'`)
      .join(", ");
    const label = missing.length === 1 ? singular : plural;
    throw new ComposeError(`no such ${label} in the distribution: ${names}`);
  }
}

/**
 * Return `[key, spec, selectedNames]` triples sorted by repo key.
 *
 * The three optional filters (`tags`, `packages`, `repository`) are ANDed;
 * omit all to select the whole distribution. An explicit `repository` key or
 * `packages` name absent from the *whole* distribution throws `ComposeError`
 * (so a typo never hides behind an empty result). Mirrors
 * `compose.select_repositories`.
 */
export function selectRepositories(
  distribution,
  { tags = null, packages = null, repository = null } = {},
) {
  const allRepos = (distribution && distribution.repositories) || {};
  const wantedTags = new Set(tags || []);
  const wantedPkgs = new Set(packages || []);
  const wantedRepos = new Set(repository || []);

  const knownPkgs = new Set();
  for (const spec of Object.values(allRepos)) {
    const specPkgs = spec && spec.packages;
    if (isMapping(specPkgs)) {
      for (const name of Object.keys(specPkgs)) knownPkgs.add(name);
    }
  }
  rejectUnknown(
    "repository entry",
    "repository entries",
    [...wantedRepos].filter((r) => !(r in allRepos)),
  );
  rejectUnknown(
    "package",
    "packages",
    [...wantedPkgs].filter((p) => !knownPkgs.has(p)),
  );

  const selected = [];
  for (const key of Object.keys(allRepos).sort(cmp)) {
    if (wantedRepos.size && !wantedRepos.has(key)) continue;
    const spec = allRepos[key] || {};
    const specPkgs = (spec && spec.packages) || {};
    if (!isMapping(specPkgs)) {
      throw new ComposeError(
        `repository '${key}' has 'packages' that is not a mapping of package name to spec`,
      );
    }
    const names = Object.keys(specPkgs)
      .filter((name) => {
        const pkg = specPkgs[name] || {};
        const pkgTags = new Set((pkg && pkg.tags) || []);
        const tagOk = wantedTags.size === 0 || [...pkgTags].some((t) => wantedTags.has(t));
        const pkgOk = wantedPkgs.size === 0 || wantedPkgs.has(name);
        return tagOk && pkgOk;
      })
      .sort(cmp);
    if (names.length) selected.push([key, spec, names]);
  }
  return selected;
}

/**
 * Map selected repositories to an ordered `Map<key, {type,url,version}>`.
 *
 * The entry key is the registry repository key, so a monorepo's packages
 * collapse into one clone. Each entry carries exactly vcstool's
 * `type`/`url`/`version`; `version` is `ref.value` verbatim. Throws
 * `ComposeError` on missing `url`/`ref.value` or a non-mapping `ref`. Mirrors
 * `compose.to_repos_entries` (a Map preserves insertion order for every key).
 */
export function toReposEntries(repositories) {
  const entries = new Map();
  for (const [key, specRaw] of repositories) {
    const spec = specRaw || {};
    const url = spec.url;
    if (!url) throw new ComposeError(`repository '${key}' is missing 'url'`);
    const ref = spec.ref || {};
    if (!isMapping(ref)) {
      throw new ComposeError(
        `repository '${key}' has 'ref' that is not a mapping with 'kind' and 'value'`,
      );
    }
    const version = ref.value;
    if (!version) throw new ComposeError(`repository '${key}' is missing 'ref.value'`);
    entries.set(key, { type: "git", url, version });
  }
  return entries;
}

/**
 * Build the `# …` comment lines that precede the rendered `.repos`.
 * Mirrors `compose.provenance_header` — same order and the same conditional
 * gates (`autoware`/`generatedAt` on `!= null`; the rest on truthiness).
 */
export function provenanceHeader({
  toolVersion,
  rosDistro,
  source,
  tags = null,
  packages = null,
  repository = null,
  autoware = null,
  generatedAt = null,
  selection = null,
} = {}) {
  const lines = [
    `# aw-index-cli ${toolVersion}`,
    `# source: ${source}`,
    `# rosdistro: ${rosDistro}`,
    `# tags: ${tags && tags.length ? tags.join(", ") : "all"}`,
  ];
  if (packages && packages.length) lines.push(`# packages: ${packages.join(", ")}`);
  if (repository && repository.length) lines.push(`# repository: ${repository.join(", ")}`);
  if (autoware != null) {
    lines.push(
      `# autoware: ${autoware} (informational only — not a ref selector; the registry tracks one ref per repository)`,
    );
  }
  if (generatedAt != null) lines.push(`# generated_at: ${generatedAt}`);
  if (selection && selection.length) {
    lines.push("# selected packages by repository:");
    for (const [key, names] of selection) lines.push(`#   ${key}: ${names.join(", ")}`);
  }
  lines.push("# Generated file — re-run 'aw-index-cli compose …' to update; do not edit by hand.");
  return lines;
}

/**
 * Render the full `.repos` document (header comments + YAML body).
 * Mirrors `compose.render_repos`: `header.join("\n") + "\n" + body`, where the
 * body reproduces `yaml.safe_dump({"repositories": entries}, sort_keys=False,
 * default_flow_style=False)` exactly (see `dumpBody`/`yamlScalar`).
 */
export function renderRepos(
  distribution,
  { tags = null, packages = null, repository = null, headerLines } = {},
) {
  const repositories = selectRepositories(distribution, { tags, packages, repository });
  const entries = toReposEntries(repositories);
  return headerLines.join("\n") + "\n" + dumpBody(entries);
}

/** Provenance string mirroring `registry.describe_source` for the network default. */
export function defaultSource(repo = DEFAULT_REPO, ref = DEFAULT_REF) {
  return `${repo}@${ref}`;
}

/**
 * One-call convenience assembling header + body exactly like the CLI's
 * `compose` handler (`cli._cmd_compose`). `packages`/`tags`/`repository` are the
 * selection filters AND are recorded in the header, matching
 * `aw-index-cli compose …`. Omit `generatedAt` for deterministic output
 * (equivalent to `--no-timestamp`).
 */
export function composeReposFile(
  distribution,
  {
    rosDistro,
    source,
    toolVersion = VERSION,
    tags = null,
    packages = null,
    repository = null,
    autoware = null,
    generatedAt = null,
  } = {},
) {
  const src = source != null ? source : defaultSource();
  const selection = selectRepositories(distribution, { tags, packages, repository }).map(
    ([key, , names]) => [key, names],
  );
  const headerLines = provenanceHeader({
    toolVersion,
    rosDistro,
    source: src,
    tags,
    packages,
    repository,
    autoware,
    generatedAt,
    selection,
  });
  return renderRepos(distribution, { tags, packages, repository, headerLines });
}

/**
 * The exact `aw-index-cli compose …` command that reproduces a selection of
 * individually-picked packages. `--packages` is space-separated (`nargs="*"`).
 */
export function composeCommand({ rosDistro, packages = [] } = {}) {
  const names = [...new Set(packages)].sort(cmp);
  const pkgArg = names.length ? ` --packages ${names.join(" ")}` : "";
  return `aw-index-cli compose --rosdistro ${rosDistro}${pkgArg} --stdout | vcs import src`;
}

// ---------------------------------------------------------------------------
// YAML emission — faithful to PyYAML 6.x `safe_dump(default_flow_style=False)`.
// ---------------------------------------------------------------------------

// PyYAML's implicit resolver patterns (SafeLoader), ported verbatim with the
// re.X whitespace stripped. A plain scalar matching any of these would reload
// as a non-string, so it must be single-quoted to stay a string.
const RESOLVERS = [
  /^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$/,
  /^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$/,
  /^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$/,
  /^(?:~|null|Null|NULL|)$/,
  /^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$/,
  /^(?:<<)$/,
  /^(?:=)$/,
];

// Characters that can never begin a plain scalar (PyYAML `analyze_scalar`).
const LEADING_INDICATORS = new Set([
  " ",
  "#",
  ",",
  "[",
  "]",
  "{",
  "}",
  "&",
  "*",
  "!",
  "|",
  ">",
  "@",
  "`",
  '"',
  "'",
  "%",
]);

// True if the scalar holds an ASCII control char (0x00–0x1F or 0x7F), which
// PyYAML would force out of plain style. Written arithmetically (no escapes) so
// the source stays clean ASCII. Registry refs/URLs never contain these.
function hasControlChar(s) {
  for (let i = 0; i < s.length; i += 1) {
    const c = s.charCodeAt(i);
    if (c < 0x20 || c === 0x7f) return true;
  }
  return false;
}

function needsPlainQuoting(s) {
  const first = s[0];
  if (LEADING_INDICATORS.has(first)) return true;
  // '-', '?', ':' are indicators only when followed by a space or at end.
  if ((first === "-" || first === "?" || first === ":") && (s.length === 1 || s[1] === " ")) {
    return true;
  }
  if (s[s.length - 1] === " ") return true;
  if (s.includes(": ")) return true;
  if (s.includes(" #")) return true;
  if (hasControlChar(s)) return true;
  return false;
}

/**
 * Render one scalar as PyYAML's `safe_dump` would: plain when it is safe and
 * reloads as a string, otherwise single-quoted (with `'` doubled). This is the
 * one subtle piece — e.g. a numeric-looking tag `1.20`, or a branch `on`/`no`,
 * must be quoted so it round-trips as a string. Exported for unit testing.
 */
export function yamlScalar(value) {
  const s = String(value);
  if (s === "") return "''";
  if (RESOLVERS.some((re) => re.test(s)) || needsPlainQuoting(s)) {
    return `'${s.replace(/'/g, "''")}'`;
  }
  return s;
}

// Reproduce `yaml.safe_dump({"repositories": entries})` for our fixed shape:
// a `repositories:` mapping keyed by repo key, each value a 3-field mapping.
function dumpBody(entries) {
  if (entries.size === 0) return "repositories: {}\n";
  let out = "repositories:\n";
  for (const [key, entry] of entries) {
    out += `  ${yamlScalar(key)}:\n`;
    out += `    type: ${yamlScalar(entry.type)}\n`;
    out += `    url: ${yamlScalar(entry.url)}\n`;
    out += `    version: ${yamlScalar(entry.version)}\n`;
  }
  return out;
}
