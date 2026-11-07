import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_PAYOUT_MESSAGE_LENGTH,
  composerMessageText,
  countCharacters,
  countComposerMessage,
  fitComposerToLimit,
  fitInsertion,
  truncateCharacters,
} from "../../frontend/js/message-limit.mjs";

test("message length counts Unicode code points, emoji, newlines, and the displayed period", () => {
  const composer = {
    messageBefore: "🙂\n",
    periodStart: "01.02",
    periodEnd: "28.02",
    messageAfter: "\nГотово",
  };

  assert.equal(composerMessageText(composer), "🙂\n01.02 – 28.02\nГотово");
  assert.equal(countComposerMessage(composer), Array.from(composerMessageText(composer)).length);
  assert.equal(countCharacters("🙂\n"), 2);
});

test("paste is truncated to the available length without splitting a surrogate pair", () => {
  const result = fitInsertion({
    currentText: "x".repeat(MAX_PAYOUT_MESSAGE_LENGTH - 15),
    start: MAX_PAYOUT_MESSAGE_LENGTH - 15,
    end: MAX_PAYOUT_MESSAGE_LENGTH - 15,
    insertedText: "🙂ab",
    otherText: "",
    periodText: "01.02 – 28.02",
  });

  assert.equal(result.acceptedText, "🙂a");
  assert.equal(result.truncated, true);
  assert.equal(countCharacters(result.text) + countCharacters("01.02 – 28.02"), MAX_PAYOUT_MESSAGE_LENGTH);
});

test("replacing a selection uses the removed characters as available space", () => {
  const otherText = "z".repeat(MAX_PAYOUT_MESSAGE_LENGTH - 17);
  const result = fitInsertion({
    currentText: "A🙂B",
    start: 1,
    end: 3,
    insertedText: "🙂x",
    otherText,
    periodText: "01.02 – 28.02",
  });

  assert.equal(result.text, "A🙂xB");
  assert.equal(result.truncated, false);
  assert.equal(countCharacters(result.text) + countCharacters(otherText) + countCharacters("01.02 – 28.02"), MAX_PAYOUT_MESSAGE_LENGTH);
});

test("period placeholders count toward the initial editor total", () => {
  assert.equal(
    countComposerMessage({ messageBefore: "", periodStart: "", periodEnd: "", messageAfter: "" }),
    countCharacters("ДД.ММ – ДД.ММ"),
  );
});

test("when a date edit expands the displayed period, message text is clipped to fit", () => {
  const composer = {
    messageBefore: "a".repeat(1000),
    periodStart: "1",
    periodEnd: "2",
    messageAfter: "b".repeat(2490),
  };
  const expandedComposer = { ...composer, periodStart: "01.02", periodEnd: "28.02" };
  const fitted = fitComposerToLimit(expandedComposer);
  const completedDates = {
    ...expandedComposer,
    ...fitted,
  };

  assert.equal(countComposerMessage(completedDates), MAX_PAYOUT_MESSAGE_LENGTH);
  assert.equal(fitted.messageBefore, composer.messageBefore);
  assert.equal(fitted.messageAfter.length, 2487);
});

test("truncation never returns half of a Unicode code point", () => {
  assert.equal(truncateCharacters("a🙂b", 2), "a🙂");
});
