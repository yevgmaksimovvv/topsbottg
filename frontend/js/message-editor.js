import { COMPOSER_IDS } from "./constants.js";
import { renderApp } from "./render-app.js";
import {
  MAX_PAYOUT_MESSAGE_LENGTH,
  PAYOUT_PERIOD_RESERVED_LENGTH,
  countCharacters,
  fitInsertion,
} from "./message-limit.js";
import { state, syncComposerFields } from "./store.js";

const composingEditors = new WeakSet();
const inputSnapshots = new WeakMap();

function otherText(key) {
  return state.composer[key === "messageBefore" ? "messageAfter" : "messageBefore"];
}

function insertionFit(editor, insertedText, start = editor.selectionStart, end = editor.selectionEnd) {
  const key = editor.dataset.messagePart;
  return fitInsertion({
    currentText: editor.value,
    start,
    end,
    insertedText,
    otherText: otherText(key),
  });
}

function inputText(event) {
  if (event.inputType === "insertParagraph" || event.inputType === "insertLineBreak") return "\n";
  if (event.dataTransfer) return event.dataTransfer.getData("text/plain");
  return typeof event.data === "string" ? event.data : null;
}

function replaceSelection(editor, text) {
  const { selectionStart: start, selectionEnd: end } = editor;
  editor.focus();
  editor.setSelectionRange(start, end);
  if (document.execCommand("insertText", false, text)) return;

  editor.setRangeText(text, start, end, "end");
  updateEditor(editor, false);
}

function snapshot(editor) {
  return {
    text: editor.value,
    start: editor.selectionStart,
    end: editor.selectionEnd,
  };
}

function clampAfterInput(editor, previous) {
  const nextText = editor.value;
  const key = editor.dataset.messagePart;
  if (
    countCharacters(nextText) + countCharacters(otherText(key)) + PAYOUT_PERIOD_RESERVED_LENGTH <= MAX_PAYOUT_MESSAGE_LENGTH
  ) return;

  const oldText = previous?.text ?? state.composer[key];
  let prefixLength = 0;
  while (prefixLength < oldText.length && prefixLength < nextText.length && oldText[prefixLength] === nextText[prefixLength]) {
    prefixLength += 1;
  }
  let suffixLength = 0;
  while (
    suffixLength < oldText.length - prefixLength &&
    suffixLength < nextText.length - prefixLength &&
    oldText[oldText.length - suffixLength - 1] === nextText[nextText.length - suffixLength - 1]
  ) {
    suffixLength += 1;
  }

  const oldEnd = oldText.length - suffixLength;
  const newEnd = nextText.length - suffixLength;
  const insertedText = nextText.slice(prefixLength, newEnd);
  const fitted = fitInsertion({
    currentText: oldText,
    start: prefixLength,
    end: oldEnd,
    insertedText,
    otherText: otherText(key),
  });
  const cursorDelta = insertedText.length - fitted.acceptedText.length;
  const cursor = Math.max(prefixLength, editor.selectionStart - cursorDelta);
  editor.setRangeText(fitted.text, 0, editor.value.length, "preserve");
  editor.setSelectionRange(Math.min(cursor, fitted.text.length), Math.min(cursor, fitted.text.length));
}

function updateEditor(editor, isComposing) {
  const key = editor.dataset.messagePart;
  if (!isComposing) {
    clampAfterInput(editor, inputSnapshots.get(editor));
    inputSnapshots.delete(editor);
  }
  state.composer[key] = editor.value;
  syncComposerFields();
  renderApp();
}

function bindEditor(editor) {
  editor.addEventListener("compositionstart", () => {
    composingEditors.add(editor);
    inputSnapshots.set(editor, snapshot(editor));
  });

  editor.addEventListener("compositionend", () => {
    composingEditors.delete(editor);
    window.setTimeout(() => updateEditor(editor, false), 0);
  });

  editor.addEventListener("beforeinput", (event) => {
    const composing = event.isComposing || composingEditors.has(editor) || event.inputType.includes("Composition");
    if (composing || !event.inputType.startsWith("insert")) return;

    const text = inputText(event);
    if (text === null) return;
    inputSnapshots.set(editor, snapshot(editor));
    const fit = insertionFit(editor, text);
    if (fit.truncated) {
      event.preventDefault();
      replaceSelection(editor, fit.acceptedText);
    }
  });

  editor.addEventListener("paste", (event) => {
    event.preventDefault();
    const text = event.clipboardData?.getData("text/plain") || "";
    inputSnapshots.set(editor, snapshot(editor));
    replaceSelection(editor, insertionFit(editor, text).acceptedText);
  });

  editor.addEventListener("input", (event) => {
    updateEditor(editor, event.isComposing || composingEditors.has(editor));
  });
}

export function bindMessageEditors() {
  Object.values(COMPOSER_IDS).forEach((ids) => {
    [ids.messageBefore, ids.messageAfter].forEach((id) => {
      const editor = document.getElementById(id);
      if (editor) bindEditor(editor);
    });
  });
}
