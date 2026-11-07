export const MAX_PAYOUT_MESSAGE_LENGTH = 3500;

export function countCharacters(value) {
  return Array.from(value).length;
}

export function truncateCharacters(value, limit) {
  const safeLimit = Math.max(0, limit);
  return Array.from(value).slice(0, safeLimit).join("");
}

export function composerPeriodLabel(composer) {
  return `${composer.periodStart || "ДД.ММ"} – ${composer.periodEnd || "ДД.ММ"}`;
}

export function composerMessageText(composer) {
  return `${composer.messageBefore}${composerPeriodLabel(composer)}${composer.messageAfter}`;
}

export function countComposerMessage(composer) {
  return countCharacters(composerMessageText(composer));
}

export function fitComposerToLimit(composer) {
  let messageBefore = composer.messageBefore;
  let messageAfter = composer.messageAfter;
  let excess = countComposerMessage(composer) - MAX_PAYOUT_MESSAGE_LENGTH;
  if (excess <= 0) return { messageBefore, messageAfter };

  const afterLength = countCharacters(messageAfter);
  const keptAfterLength = Math.max(0, afterLength - excess);
  messageAfter = truncateCharacters(messageAfter, keptAfterLength);
  excess -= afterLength - keptAfterLength;
  if (excess > 0) {
    messageBefore = truncateCharacters(messageBefore, countCharacters(messageBefore) - excess);
  }
  return { messageBefore, messageAfter };
}

export function fitInsertion({ currentText, start, end, insertedText, otherText, periodText }) {
  const prefix = currentText.slice(0, start);
  const suffix = currentText.slice(end);
  const available =
    MAX_PAYOUT_MESSAGE_LENGTH -
    countCharacters(prefix) -
    countCharacters(suffix) -
    countCharacters(otherText) -
    countCharacters(periodText);
  const acceptedText = truncateCharacters(insertedText, available);

  return {
    text: `${prefix}${acceptedText}${suffix}`,
    acceptedText,
    truncated: acceptedText !== insertedText,
  };
}
