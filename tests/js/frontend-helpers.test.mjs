import assert from "node:assert/strict";
import test from "node:test";
import { parsePeriodInput, plural } from "../../frontend/js/utils.js";
import { formatPayoutPeriodLabel } from "../../frontend/js/utils.js";

test("period input accepts real day and month values, including leap day", () => {
  assert.deepEqual(parsePeriodInput("29.02"), { day: 29, month: 2 });
  assert.deepEqual(parsePeriodInput("31.04"), null);
  assert.deepEqual(parsePeriodInput("00.12"), null);
  assert.deepEqual(parsePeriodInput("1.12"), null);
  assert.deepEqual(parsePeriodInput("12.13"), null);
});

test("Russian recipient plural forms handle teens and compound counts", () => {
  const forms = (n) => plural(n, "получатель", "получателя", "получателей");
  assert.deepEqual(
    [1, 2, 5, 11, 21, 24, 25].map((n) => `${n} ${forms(n)}`),
    [
      "1 получатель",
      "2 получателя",
      "5 получателей",
      "11 получателей",
      "21 получатель",
      "24 получателя",
      "25 получателей",
    ],
  );
});

test("same-day payout uses one date in a compact label", () => {
  assert.equal(formatPayoutPeriodLabel(1, 11, 1, 11), "01.11");
  assert.equal(formatPayoutPeriodLabel(1, 11, 7, 11), "01.11 – 07.11");
});
