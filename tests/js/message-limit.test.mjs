import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_PAYOUT_MESSAGE_LENGTH,
  PAYOUT_PERIOD_RESERVED_LENGTH,
  composerMessageText,
  countCharacters,
  countComposerMessage,
  fitInsertion,
  truncateCharacters,
} from "../../frontend/js/message-limit.js";

const period = "01.02 \u2013 28.02";

test("message length counts Unicode code points, emoji, newlines, and the displayed period", () => {
  const composer = {
    messageBefore: "\u{1f642}\n",
    periodStart: "01.02",
    periodEnd: "28.02",
    messageAfter: "\n\u0413\u043e\u0442\u043e\u0432\u043e",
  };

  assert.equal(composerMessageText(composer), `\u{1f642}\n${period}\n\u0413\u043e\u0442\u043e\u0432\u043e`);
  assert.equal(countComposerMessage(composer), Array.from(composerMessageText(composer)).length);
  assert.equal(countCharacters("\u{1f642}\n"), 2);
});

test("paste is truncated to the available length without splitting a surrogate pair", () => {
  const currentText = "x".repeat(MAX_PAYOUT_MESSAGE_LENGTH - PAYOUT_PERIOD_RESERVED_LENGTH - 2);
  const result = fitInsertion({
    currentText,
    start: currentText.length,
    end: currentText.length,
    insertedText: "\u{1f642}ab",
    otherText: "",
  });

  assert.equal(result.acceptedText, "\u{1f642}a");
  assert.equal(result.truncated, true);
  assert.equal(countCharacters(result.text) + PAYOUT_PERIOD_RESERVED_LENGTH, MAX_PAYOUT_MESSAGE_LENGTH);
});

test("replacing a selection uses the removed characters as available space", () => {
  const otherText = "z".repeat(MAX_PAYOUT_MESSAGE_LENGTH - PAYOUT_PERIOD_RESERVED_LENGTH - 4);
  const result = fitInsertion({
    currentText: "A\u{1f642}B",
    start: 1,
    end: 3,
    insertedText: "\u{1f642}x",
    otherText,
  });

  assert.equal(result.text, "A\u{1f642}xB");
  assert.equal(result.truncated, false);
  assert.equal(countCharacters(result.text) + countCharacters(otherText) + PAYOUT_PERIOD_RESERVED_LENGTH, MAX_PAYOUT_MESSAGE_LENGTH);
});

test("period placeholders count toward the initial editor total", () => {
  assert.equal(
    countComposerMessage({ messageBefore: "", periodStart: "", periodEnd: "", messageAfter: "" }),
    countCharacters("\u0414\u0414.\u041c\u041c \u2013 \u0414\u0414.\u041c\u041c"),
  );
});

test("editing partial dates always reserves all 13 period characters", () => {
  const composer = {
    messageBefore: "a".repeat(1000),
    periodStart: "1",
    periodEnd: "2",
    messageAfter: "b".repeat(2487),
  };
  const completedDates = { ...composer, periodStart: "01.02", periodEnd: "28.02" };
  const result = fitInsertion({
    currentText: composer.messageAfter,
    start: composer.messageAfter.length,
    end: composer.messageAfter.length,
    insertedText: "x",
    otherText: composer.messageBefore,
  });

  assert.equal(PAYOUT_PERIOD_RESERVED_LENGTH, 13);
  assert.equal(countComposerMessage(composer), 3500);
  assert.equal(countComposerMessage(completedDates), 3500);
  assert.equal(result.acceptedText, "");
  assert.equal(composer.messageBefore, completedDates.messageBefore);
  assert.equal(composer.messageAfter, completedDates.messageAfter);
});

test("counter and available input budget agree while dates are partially filled", () => {
  const composer = {
    messageBefore: "a".repeat(1000),
    periodStart: "1",
    periodEnd: "2",
    messageAfter: "b".repeat(1000),
  };
  const available = MAX_PAYOUT_MESSAGE_LENGTH - countComposerMessage(composer);
  const insertion = fitInsertion({
    currentText: composer.messageAfter,
    start: composer.messageAfter.length,
    end: composer.messageAfter.length,
    insertedText: "x".repeat(available + 1),
    otherText: composer.messageBefore,
  });

  assert.equal(available, 1487);
  assert.equal(insertion.acceptedText.length, available);
  assert.equal(countComposerMessage({ ...composer, messageAfter: insertion.text }), MAX_PAYOUT_MESSAGE_LENGTH);
});

test("textarea values preserve one trailing newline in the final message", () => {
  const beforePeriod = {
    messageBefore: "a\n",
    periodStart: "01.02",
    periodEnd: "28.02",
    messageAfter: "",
  };
  const afterPeriod = { ...beforePeriod, messageBefore: "", messageAfter: "a\n" };

  assert.equal(composerMessageText(beforePeriod), `a\n${period}`);
  assert.equal(composerMessageText(afterPeriod), `${period}a\n`);
  assert.equal(countComposerMessage(afterPeriod), Array.from(composerMessageText(afterPeriod)).length);
});

test("truncation never returns half of a Unicode code point", () => {
  assert.equal(truncateCharacters("a\u{1f642}b", 2), "a\u{1f642}");
});
