// The stable 'checks' job must reject silently skipped mandatory suites.
// GitHub itself treats a conditionally skipped job as successful for branch protection.
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const suites = ["lint","python","postgres","browser","migrations","package"];

export function verifyResults(changeDetection, required, results) {
  const problems = [];
  if (changeDetection !== "success") {
    problems.push(`Change detection must succeed (got: ${changeDetection || "missing"})`);
  }
  for (const suite of suites) {
    const selected = required[suite];
    const actual = results[suite];
    if (selected !== "true" && selected !== "false") {
      problems.push(`${suite}: invalid or missing selection flag (${selected || "missing"})`);
      continue;
    }
    const expected = selected === "true" ? "success" : "skipped";
    if (actual !== expected) {
      problems.push(`${suite}: selected=${selected}, expected=${expected}, actual=${actual || "missing"}`);
    }
  }
  return problems;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const required = Object.fromEntries(suites.map((name) =>
    [name, process.env[`RUN_${name.toUpperCase()}`]]));
  const results = Object.fromEntries(suites.map((name) =>
    [name, process.env[`RESULT_${name.toUpperCase()}`]]));
  const errors = verifyResults(process.env.CHANGES_RESULT, required, results);
  for (const suite of suites) {
    console.log(`${suite}: required=${required[suite]}, result=${results[suite]}`);
  }
  if (errors.length) {
    for (const error of errors) console.error(`::error::${error}`);
    process.exitCode = 1;
  } else {
    console.log("All selected checks passed; irrelevant jobs were intentionally skipped.");
  }
}
