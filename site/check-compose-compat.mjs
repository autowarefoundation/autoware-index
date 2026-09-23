// Verify that a fetched compose module can select schema v4 reference-design
// roots and include their registered dependencies before Pages deploys it.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const modulePath = process.argv[2];
if (!modulePath) throw new Error("usage: node site/check-compose-compat.mjs <compose.mjs>");

const { composeReposFile, selectRepositories } = await import(pathToFileURL(modulePath).href);
const distribution = {
  schema_version: "4",
  repositories: {
    root: {
      url: "https://example.com/root",
      ref: { kind: "branch", value: "main" },
      reference_design: true,
      packages: { root_pkg: { index_dependencies: ["dependency_pkg"] } },
    },
    dependency: {
      url: "https://example.com/dependency",
      ref: { kind: "branch", value: "main" },
      packages: { dependency_pkg: {} },
    },
    unrelated: {
      url: "https://example.com/unrelated",
      ref: { kind: "branch", value: "main" },
      packages: { unrelated_pkg: {} },
    },
  },
};

assert.deepEqual(
  selectRepositories(distribution, { referenceDesign: true }).map(([key]) => key),
  ["dependency", "root"],
);

const output = composeReposFile(distribution, {
  rosDistro: "jazzy",
  source: "compatibility check",
  referenceDesign: true,
});
assert.match(output, /\n  dependency:\n/);
assert.match(output, /\n  root:\n/);
assert.doesNotMatch(output, /\n  unrelated:\n/);
