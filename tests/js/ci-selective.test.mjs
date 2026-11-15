import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import { suites, verifyResults } from "../../scripts/ci/check-results.mjs";

function policy(enabled) {
  const selected = new Set(enabled);
  const required = Object.fromEntries(suites.map((name) =>
    [name, selected.has(name) ? "true" : "false"]));
  const results = Object.fromEntries(suites.map((name) =>
    [name, selected.has(name) ? "success" : "skipped"]));
  return { required, results };
}

const scenarios = {
  "documentation only: skip all expensive checks": [],
  "frontend only: lint, browser and Nginx/Docker without PostgreSQL": ["lint", "browser", "package"],
  "backend only: Python, PostgreSQL, lint and package": ["lint", "python", "postgres", "package"],
  "migration change: full database path without browser": ["lint", "python", "postgres", "migrations", "package"],
  "manual deployment: require every suite": suites,
  "unknown file: fail-safe full suite": suites,
};

for (const [name, enabled] of Object.entries(scenarios)) {
  test(name, () => {
    const { required, results } = policy(enabled);
    assert.deepEqual(verifyResults("success", required, results), []);
  });
}

test("a required job is never allowed to be skipped", () => {
  const { required, results } = policy(["python", "postgres"]);
  results.postgres = "skipped";
  assert.match(verifyResults("success", required, results).join(" "), /postgres:.*expected=success/);
});

test("a failing or cancelled job is never accepted as an intentional skip", () => {
  const { required, results } = policy([]);
  results.browser = "failure";
  assert.match(verifyResults("success", required, results).join(" "), /browser:.*expected=skipped/);
  results.browser = "cancelled";
  assert.match(verifyResults("success", required, results).join(" "), /browser:.*expected=skipped/);
});

test("missing or invalid change-detection output fails closed", () => {
  const { required, results } = policy([]);
  assert.match(verifyResults("failure", required, results).join(" "), /Change detection must succeed/);
  required.python = "";
  assert.match(verifyResults("success", required, results).join(" "), /invalid or missing selection/);
});

test("documentation-only pushes skip production deployment", async () => {
  const workflow = await readFile(
    new URL("../../.github/workflows/ci.yml", import.meta.url), "utf8",
  );
  assert.match(workflow, /needs: \[checks, changes\]/u);
  assert.match(workflow, /github\.event_name == 'workflow_dispatch' \|\|/u);
  for (const suite of suites) {
    assert.ok(
      workflow.includes("needs.changes.outputs." + suite + " == 'true'"),
      "Missing deploy gate for " + suite,
    );
  }
});

test("all previously selected jobs are still required in a mixed change", () => {
  const { required, results } = policy(["lint", "python", "postgres", "browser", "package"]);
  results.python = "failure";
  assert.match(verifyResults("success", required, results).join(" "), /python:.*actual=failure/);
});
