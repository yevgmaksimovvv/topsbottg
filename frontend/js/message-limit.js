export const MAX_PAYOUT_MESSAGE_LENGTH = 3500,
  PAYOUT_PERIOD_RESERVED_LENGTH = 13;
export function countCharacters(value) {
  return Array.from(value).length;
}
export function truncateCharacters(value, limit) {
  return Array.from(value).slice(0, Math.max(0, limit)).join("");
}
export function composerPeriodLabel(c) {
  return `${c.periodStart || "ДД.ММ"} – ${c.periodEnd || "ДД.ММ"}`;
}
export function composerMessageText(c) {
  return `${c.messageBefore}${composerPeriodLabel(c)}${c.messageAfter}`;
}
export function countComposerMessage(c) {
  return (
    countCharacters(c.messageBefore) +
    PAYOUT_PERIOD_RESERVED_LENGTH +
    countCharacters(c.messageAfter)
  );
}
export function fitInsertion({
  currentText,
  start,
  end,
  insertedText,
  otherText,
}) {
  const prefix = currentText.slice(0, start),
    suffix = currentText.slice(end);
  const available =
    MAX_PAYOUT_MESSAGE_LENGTH -
    countCharacters(prefix) -
    countCharacters(suffix) -
    countCharacters(otherText) -
    PAYOUT_PERIOD_RESERVED_LENGTH;
  const acceptedText = truncateCharacters(insertedText, available);
  return {
    text: `${prefix}${acceptedText}${suffix}`,
    acceptedText,
    truncated: acceptedText !== insertedText,
  };
}
