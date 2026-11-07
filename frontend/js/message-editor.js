import { COMPOSER_IDS } from "./constants.js";
import { renderApp } from "./render-app.js";
import {
  MAX_PAYOUT_MESSAGE_LENGTH,
  composerPeriodLabel,
  countCharacters,
  fitInsertion,
  truncateCharacters,
} from "./message-limit.mjs";
import { state, syncComposerFields } from "./store.js";

const composingEditors = new WeakSet();
const inputSnapshots = new WeakMap();

function editorText(editor) {
  return textFromNode(editor).replace(/\r\n?/g, "\n");
}

function textFromNode(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || "";
  if (node.nodeName === "BR") return "\n";

  let text = "";
  for (const child of node.childNodes) {
    const isBlock = child.nodeType === Node.ELEMENT_NODE && ["DIV", "P"].includes(child.tagName);
    if (isBlock && text && !text.endsWith("\n")) text += "\n";
    text += textFromNode(child);
  }
  return text;
}

function selectionOffset(editor, node, offset) {
  const range = document.createRange();
  range.selectNodeContents(editor);
  range.setEnd(node, offset);
  return textFromNode(range.cloneContents()).length;
}

function selectionOffsets(editor) {
  const selection = window.getSelection();
  if (!selection?.anchorNode || !editor.contains(selection.anchorNode)) {
    const end = editorText(editor).length;
    return { start: end, end, anchor: end, focus: end };
  }

  const anchor = selectionOffset(editor, selection.anchorNode, selection.anchorOffset);
  const focus = selectionOffset(editor, selection.focusNode, selection.focusOffset);
  return {
    start: Math.min(anchor, focus),
    end: Math.max(anchor, focus),
    anchor,
    focus,
  };
}

function clampOffset(text, offset) {
  let result = Math.max(0, Math.min(offset, text.length));
  if (result > 0 && result < text.length) {
    const previous = text.charCodeAt(result - 1);
    const next = text.charCodeAt(result);
    if (previous >= 0xd800 && previous <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) result -= 1;
  }
  return result;
}

function setEditorSelection(editor, anchorOffset, focusOffset) {
  const text = editorText(editor);
  if (editor.childNodes.length !== 1 || editor.firstChild.nodeType !== Node.TEXT_NODE) {
    editor.textContent = text;
  }
  let textNode = editor.firstChild;
  if (!textNode) {
    editor.append(document.createTextNode(""));
    textNode = editor.firstChild;
  }
  const anchor = clampOffset(text, anchorOffset);
  const focus = clampOffset(text, focusOffset);
  const selection = window.getSelection();
  if (!selection) return;

  if (typeof selection.setBaseAndExtent === "function") {
    selection.setBaseAndExtent(textNode, anchor, textNode, focus);
    return;
  }
  const range = document.createRange();
  range.setStart(textNode, Math.min(anchor, focus));
  range.setEnd(textNode, Math.max(anchor, focus));
  selection.removeAllRanges();
  selection.addRange(range);
}

function otherText(key) {
  return state.composer[key === "messageBefore" ? "messageAfter" : "messageBefore"];
}

function descriptorFor(editor) {
  return { key: editor.dataset.messagePart };
}

function insertionFit(editor, insertedText, offsets = selectionOffsets(editor)) {
  const descriptor = descriptorFor(editor);
  return fitInsertion({
    currentText: editorText(editor),
    start: offsets.start,
    end: offsets.end,
    insertedText,
    otherText: otherText(descriptor.key),
    periodText: composerPeriodLabel(state.composer),
  });
}

function snapshotFor(editor) {
  return { text: editorText(editor), ...selectionOffsets(editor) };
}

function selectInsertionRange(editor, start, end) {
  const text = editorText(editor);
  if (editor.childNodes.length !== 1 || editor.firstChild.nodeType !== Node.TEXT_NODE) {
    editor.textContent = text;
  }
  if (!editor.firstChild) editor.append(document.createTextNode(""));
  const textNode = editor.firstChild;
  const range = document.createRange();
  range.setStart(textNode, clampOffset(text, start));
  range.setEnd(textNode, clampOffset(text, end));
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

function insertText(editor, text, offsets) {
  const snapshot = snapshotFor(editor);
  inputSnapshots.set(editor, snapshot);
  selectInsertionRange(editor, offsets.start, offsets.end);
  const inserted = document.execCommand("insertText", false, text);
  if (!inserted) {
    const range = window.getSelection()?.getRangeAt(0);
    if (range) {
      range.deleteContents();
      const textNode = document.createTextNode(text);
      range.insertNode(textNode);
      range.setStartAfter(textNode);
      range.collapse(true);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
    updateEditor(editor, false);
  }
}

function allowedInsertionText(editor, insertionText, offsets) {
  return insertionFit(editor, insertionText, offsets).acceptedText;
}

function inputText(event) {
  if (event.inputType === "insertParagraph" || event.inputType === "insertLineBreak") return "\n";
  if (event.dataTransfer) return event.dataTransfer.getData("text/plain");
  return typeof event.data === "string" ? event.data : null;
}

function clampAfterInput(editor, snapshot) {
  const nextText = editorText(editor);
  const descriptor = descriptorFor(editor);
  if (countCharacters(nextText) + countCharacters(otherText(descriptor.key)) + countCharacters(composerPeriodLabel(state.composer)) <= MAX_PAYOUT_MESSAGE_LENGTH) {
    return nextText;
  }

  const oldText = snapshot?.text ?? state.composer[descriptor.key];
  let prefixLength = 0;
  while (prefixLength < oldText.length && prefixLength < nextText.length && oldText[prefixLength] === nextText[prefixLength]) {
    prefixLength += 1;
  }
  prefixLength = clampOffset(oldText, prefixLength);

  let suffixLength = 0;
  while (
    suffixLength < oldText.length - prefixLength &&
    suffixLength < nextText.length - prefixLength &&
    oldText[oldText.length - suffixLength - 1] === nextText[nextText.length - suffixLength - 1]
  ) {
    suffixLength += 1;
  }
  if (suffixLength > 0) {
    const oldSuffixStart = clampOffset(oldText, oldText.length - suffixLength);
    suffixLength = oldText.length - oldSuffixStart;
  }

  const oldEnd = oldText.length - suffixLength;
  const newEnd = nextText.length - suffixLength;
  const insertedText = nextText.slice(prefixLength, newEnd);
  const fitted = fitInsertion({
    currentText: oldText,
    start: prefixLength,
    end: oldEnd,
    insertedText,
    otherText: otherText(descriptor.key),
    periodText: composerPeriodLabel(state.composer),
  });
  let finalText = fitted.text;
  if (countCharacters(finalText) + countCharacters(otherText(descriptor.key)) + countCharacters(composerPeriodLabel(state.composer)) > MAX_PAYOUT_MESSAGE_LENGTH) {
    const budget = MAX_PAYOUT_MESSAGE_LENGTH - countCharacters(otherText(descriptor.key)) - countCharacters(composerPeriodLabel(state.composer));
    finalText = truncateCharacters(finalText, budget);
  }

  const currentSelection = selectionOffsets(editor);
  const mapOffset = (offset) => {
    if (offset <= prefixLength) return offset;
    if (offset >= newEnd) return offset - (insertedText.length - fitted.acceptedText.length);
    return prefixLength + fitted.acceptedText.length;
  };
  const anchor = mapOffset(currentSelection.anchor);
  const focus = mapOffset(currentSelection.focus);
  editor.textContent = finalText;
  setEditorSelection(editor, anchor, focus);
  return finalText;
}

function updateEditor(editor, isComposing) {
  const descriptor = descriptorFor(editor);
  let text = editorText(editor);
  state.composer[descriptor.key] = text;

  if (!isComposing) {
    text = clampAfterInput(editor, inputSnapshots.get(editor));
    state.composer[descriptor.key] = text;
    inputSnapshots.delete(editor);
  }

  syncComposerFields();
  renderApp();
}

function bindEditor(editor) {
  editor.addEventListener("compositionstart", () => {
    composingEditors.add(editor);
    inputSnapshots.set(editor, snapshotFor(editor));
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
    const offsets = selectionOffsets(editor);
    const fit = insertionFit(editor, text, offsets);
    inputSnapshots.set(editor, snapshotFor(editor));

    if (fit.truncated || event.inputType === "insertParagraph" || event.inputType === "insertLineBreak") {
      event.preventDefault();
      insertText(editor, fit.acceptedText, offsets);
    }
  });

  editor.addEventListener("paste", (event) => {
    event.preventDefault();
    const text = event.clipboardData?.getData("text/plain") || "";
    const offsets = selectionOffsets(editor);
    insertText(editor, allowedInsertionText(editor, text, offsets), offsets);
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
