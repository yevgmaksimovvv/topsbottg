import {
  MAX_PAYOUT_MESSAGE_LENGTH,
  PAYOUT_PERIOD_RESERVED_LENGTH,
  countCharacters,
  fitInsertion,
} from "./message-limit.js";
import { state, $ } from "./store.js";
const composing = new WeakSet(),
  snapshots = new WeakMap();
const other = (k) =>
  state.composer[k === "messageBefore" ? "messageAfter" : "messageBefore"];
const snapshot = (editor) => ({
  text: editor.value,
  start: editor.selectionStart,
  end: editor.selectionEnd,
});
function fit(
  editor,
  text,
  start = editor.selectionStart,
  end = editor.selectionEnd,
) {
  return fitInsertion({
    currentText: editor.value,
    start,
    end,
    insertedText: text,
    otherText: other(editor.dataset.messagePart),
  });
}
function clamp(editor, old) {
  const next = editor.value,
    k = editor.dataset.messagePart;
  if (
    countCharacters(next) +
      countCharacters(other(k)) +
      PAYOUT_PERIOD_RESERVED_LENGTH <=
    MAX_PAYOUT_MESSAGE_LENGTH
  )
    return;
  let prefix = 0;
  while (
    prefix < old.text.length &&
    prefix < next.length &&
    old.text[prefix] === next[prefix]
  )
    prefix++;
  let suffix = 0;
  while (
    suffix < old.text.length - prefix &&
    suffix < next.length - prefix &&
    old.text[old.text.length - 1 - suffix] === next[next.length - 1 - suffix]
  )
    suffix++;
  const oldEnd = old.text.length - suffix,
    newEnd = next.length - suffix,
    inserted = next.slice(prefix, newEnd);
  const fixed = fitInsertion({
    currentText: old.text,
    start: prefix,
    end: oldEnd,
    insertedText: inserted,
    otherText: other(k),
  });
  const caret = Math.max(
    prefix,
    editor.selectionStart - (inserted.length - fixed.acceptedText.length),
  );
  editor.value = fixed.text;
  editor.setSelectionRange(
    Math.min(caret, fixed.text.length),
    Math.min(caret, fixed.text.length),
  );
}
function update(editor, composingNow = false) {
  const k = editor.dataset.messagePart,
    old = snapshots.get(editor);
  if (!composingNow) {
    if (old) clamp(editor, old);
    snapshots.set(editor, snapshot(editor));
  }
  state.composer[k] = editor.value;
  editor.style.height = "auto";
  editor.style.height = `${Math.min(editor.scrollHeight, 240)}px`;
  const n = $("message-count");
  if (n)
    n.textContent = `${countCharacters(state.composer.messageBefore) + PAYOUT_PERIOD_RESERVED_LENGTH + countCharacters(state.composer.messageAfter)} / ${MAX_PAYOUT_MESSAGE_LENGTH}`;
  const p = $("preview");
  if (p && !p.classList.contains("hidden"))
    p.textContent = `${state.composer.messageBefore}${state.composer.periodStart || "ДД.ММ"} – ${state.composer.periodEnd || "ДД.ММ"}${state.composer.messageAfter}`;
}
function replace(editor, text) {
  const s = editor.selectionStart,
    e = editor.selectionEnd;
  editor.setRangeText(text, s, e, "end");
  update(editor);
}
export function bindMessageEditors() {
  document.querySelectorAll("textarea[data-message-part]").forEach((editor) => {
    snapshots.set(editor, snapshot(editor));
    editor.addEventListener("compositionstart", () => {
      composing.add(editor);
      snapshots.set(editor, snapshot(editor));
    });
    editor.addEventListener("compositionend", () => {
      composing.delete(editor);
      setTimeout(() => update(editor), 0);
    });
    editor.addEventListener("beforeinput", (e) => {
      if (
        e.isComposing ||
        composing.has(editor) ||
        e.inputType?.includes("Composition") ||
        !e.inputType?.startsWith("insert")
      )
        return;
      const text =
        e.inputType === "insertLineBreak" || e.inputType === "insertParagraph"
          ? "\n"
          : (e.dataTransfer?.getData("text/plain") ?? e.data);
      if (typeof text !== "string") return;
      snapshots.set(editor, snapshot(editor));
      const result = fit(editor, text);
      if (result.truncated) {
        e.preventDefault();
        replace(editor, result.acceptedText);
      }
    });
    editor.addEventListener("paste", (e) => {
      e.preventDefault();
      snapshots.set(editor, snapshot(editor));
      replace(
        editor,
        fit(editor, e.clipboardData?.getData("text/plain") || "").acceptedText,
      );
    });
    editor.addEventListener("input", (e) =>
      update(editor, e.isComposing || composing.has(editor)),
    );
    editor.style.height = "auto";
    editor.style.height = `${Math.min(editor.scrollHeight, 240)}px`;
  });
}
