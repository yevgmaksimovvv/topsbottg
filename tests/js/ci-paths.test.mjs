import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { posix as path } from "node:path";
import { test } from "node:test";

const source = await readFile(new URL("../../.github/ci-paths.yml", import.meta.url), "utf8");

// The file uses a deliberately small YAML subset. Reject unrecognized syntax.
function readFilters(content) {
  const filters = {};
  let current;
  for (const rawLine of content.split(/\r?\n/u)) {
    if (!rawLine.trim() || rawLine.trimStart().startsWith("#")) continue;
    const group = /^([a-z][a-z0-9_-]*):$/u.exec(rawLine);
    if (group) {
      current = group[1];
      assert.equal(filters[current], undefined, `Duplicate filter: ${current}`);
      filters[current] = [];
      continue;
    }
    const entry = /^  - '([^']+)'$/u.exec(rawLine);
    assert.ok(entry && current, `Unsupported filter configuration: ${rawLine}`);
    filters[current].push(entry[1]);
  }
  return filters;
}

const filters = readFilters(source);
const suites = ["lint", "python", "postgres", "browser", "migrations", "package"];
assert.deepEqual(Object.keys(filters), ["full", ...suites, "unclassified"]);

// GitHub's paths-filter matches hidden files too; Node's matchesGlob does not.
// Mask leading dots in both the path and glob so the test uses the same policy.
function matchWithHiddenFiles(file, glob) {
  const normalize = (value) => value.split("/").map((segment) =>
    segment.startsWith(".") ? `__dot__${segment.slice(1)}` : segment
  ).join("/");
  return path.matchesGlob(normalize(file), normalize(glob));
}

function matches(file, rules) {
  const included = rules.filter((rule) => !rule.startsWith("!"));
  const excluded = rules.filter((rule) => rule.startsWith("!")).map((rule) => rule.slice(1));
  return included.some((glob) => matchWithHiddenFiles(file, glob)) &&
    !excluded.some((glob) => matchWithHiddenFiles(file, glob));
}

function jobsFor(files) {
  const changed = (category) => files.some((file) => matches(file, filters[category]));
  if (changed("full") || changed("unclassified")) return suites;
  return suites.filter((suite) => changed(suite));
}

const cases = [
  { files: ["README.md", "docs/ci.md"], expected: [] },
  { files: ["frontend/README.md"], expected: [] },
  { files: ["frontend/styles.css"], expected: ["lint", "browser", "package"] },
  { files: ["src/topsbottg/services.py"], expected: ["lint", "python", "postgres", "package"] },
  { files: ["src/topsbottg/models.py"], expected: ["lint", "python", "postgres", "migrations", "package"] },
  { files: ["migrations/versions/0011_new_table.py"], expected: ["lint", "python", "postgres", "migrations", "package"] },
  { files: ["tests/browser/frontend.test.mjs"], expected: ["lint", "browser"] },
  { files: ["tests/js/message-limit.test.mjs"], expected: ["lint"] },
  { files: ["nginx/templates/default.conf.template"], expected: ["package"] },
  { files: ["README.md", "frontend/styles.css"], expected: ["lint", "browser", "package"] },
  { files: [".github/workflows/ci.yml"], expected: suites },
  { files: [".github/ci-paths.yml"], expected: suites },
  { files: ["uv.lock"], expected: suites },
  { files: ["new-component/handler.ts"], expected: suites },
  { files: [".env.example"], expected: suites },
];

for (const { files, expected } of cases) {
  test(`selective CI paths: ${files.join(", ")}`, () => {
    assert.deepEqual(jobsFor(files), expected);
  });
}

test("new unclassified code paths trigger the full suite", () => {
  assert.deepEqual(jobsFor(["services/worker.py"]), suites);
});
