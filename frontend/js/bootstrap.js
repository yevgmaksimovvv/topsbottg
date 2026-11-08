import { SEARCH_DEBOUNCE_MS } from "./constants.js";
import { renderApp, renderNavigation } from "./render-app.js";
import {
  bindTelegramViewportEvents,
  setThemeVariables,
  syncViewportVariables,
  telegramWebApp,
} from "./telegram.js";
import {
  canUseApi,
  clearNotification,
  payoutPeriodLabel,
  refreshTelegramAuthState,
  setToast,
  state,
} from "./store.js";
import {
  loadPayouts,
  loadUsers,
  markPaid,
  clearPayoutSelection,
  selectPayout,
  startAdminEvents,
  stopAdminEvents,
} from "./api.js";
import { createPayout, sendPayout } from "./render-payouts.js";
import { bindMessageEditors } from "./message-editor.js";
import { renderRecipients } from "./render-recipients.js";
import { renderUsers } from "./render-users.js";
import { parsePeriodInput, plural } from "./utils.js";

let lastFocusedElement = null;
let goSelectionInFlight = false;
const periodComposing = new WeakSet();

function bindPeriodInput(id, stateKey) {
  const input = document.getElementById(id);
  input.addEventListener("compositionstart", () => periodComposing.add(input));
  input.addEventListener("compositionend", () => {
    periodComposing.delete(input);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  input.addEventListener("input", (event) => {
    const caret = input.selectionStart ?? input.value.length;
    const digitCount = input.value.slice(0, caret).replace(/\D/g, "").length;
    const digits = input.value.replace(/\D/g, "").slice(0, 4);
    const formatted =
      digits.length > 2 ? `${digits.slice(0, 2)}.${digits.slice(2)}` : digits;
    input.value = formatted;
    const nextCaret = Math.min(
      formatted.length,
      digitCount > 2 ? digitCount + 1 : digitCount,
    );
    input.setSelectionRange(nextCaret, nextCaret);
    state.composer[stateKey] = formatted;
    const error = document.getElementById(`${id}-error`);
    const validCompleteDate = formatted.length === 5 && Boolean(parsePeriodInput(formatted));
    error.textContent = formatted.length === 5 && !validCompleteDate ? "\u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0434\u0430\u0442\u0443." : "";
    if (!event.isComposing && !periodComposing.has(input) && validCompleteDate) input.blur();
    document.getElementById("protected-period").textContent =
      `${state.composer.periodStart || "ДД.ММ"} – ${state.composer.periodEnd || "ДД.ММ"}`;
    const preview = document.getElementById("preview");
    if (!preview.classList.contains("hidden")) {
      preview.textContent = `${state.composer.messageBefore}${state.composer.periodStart || "ДД.ММ"} – ${state.composer.periodEnd || "ДД.ММ"}${state.composer.messageAfter}`;
    }
  });
}

async function copyRecipientPaymentDetails(recipientId) {
  const recipient = state.recipients.find(
    (item) => Number(item.id) === Number(recipientId),
  );
  const snapshot = recipient?.payment_profile_snapshot;
  const text =
    typeof snapshot === "string"
      ? snapshot
      : (snapshot?.raw_payment_details ??
        (snapshot
          ? Object.entries(snapshot)
              .filter(([, value]) => value != null && value !== "")
              .map(([key, value]) => `${key}: ${value}`)
              .join("\n")
          : ""));
  try {
    await navigator.clipboard.writeText(text);
    setToast("Платёжные данные скопированы.", "success");
  } catch {
    setToast("Не удалось скопировать. Выделите данные вручную.", "warning");
  }
  renderApp();
}

function showConfirmation({
  title,
  content,
  warning = "",
  hideClose = false,
  confirmLabel,
  focusFallback = "",
  onConfirm,
}) {
  const dialog = document.getElementById("confirm-dialog");
  if (dialog.open) return;
  lastFocusedElement = document.activeElement;
  dialog.querySelector("#dialog-title").textContent = title;
  dialog.querySelector("#dialog-content").textContent = content;
  const warningNode = dialog.querySelector("#dialog-warning");
  warningNode.textContent = warning;
  warningNode.classList.toggle("hidden", !warning);
  const confirmButton = dialog.querySelector("#dialog-confirm");
  confirmButton.textContent = confirmLabel;
  confirmButton.disabled = false;
  const closeButton = dialog.querySelector(".dialog-close");
  if (closeButton) closeButton.classList.toggle("hidden", hideClose);
  dialog.querySelectorAll("[data-dialog-cancel]").forEach((button) => {
    button.onclick = () => dialog.close("cancel");
  });
  confirmButton.onclick = async () => {
    if (state.dialogBusy) return;
    state.dialogBusy = true;
    confirmButton.disabled = true;
    try {
      await onConfirm();
      if (dialog.open) dialog.close("confirm");
    } finally {
      state.dialogBusy = false;
      confirmButton.disabled = false;
    }
  };
  dialog.addEventListener(
    "close",
    () => {
      if (
        lastFocusedElement?.isConnected &&
        !lastFocusedElement.closest(".hidden, [hidden]")
      )
        lastFocusedElement.focus();
      else if (focusFallback && document.querySelector(focusFallback))
        document.querySelector(focusFallback).focus();
      else
        document
          .querySelector(`[data-mobile-view="${state.activeMobileView}"]`)
          ?.focus();
    },
    { once: true },
  );
  dialog.showModal();
  dialog.querySelector(".dialog-actions [data-dialog-cancel]").focus();
}

function bindEvents() {
  document.getElementById("recipients-list")?.addEventListener("click", (event) => {
    const copyButton = event.target.closest?.("[data-copy-recipient]");
    if (copyButton) {
      event.stopPropagation();
      void copyRecipientPaymentDetails(copyButton.dataset.copyRecipient);
      return;
    }
    const expander = event.target.closest?.("[data-expand-recipient]");
    if (!expander) return;
    const id = Number(expander.dataset.expandRecipient);
    state.expandedRecipients.has(id)
      ? state.expandedRecipients.delete(id)
      : state.expandedRecipients.add(id);
    renderRecipients();
  });
  document.addEventListener("input", (event) => {
    if (event.target.id !== "users-search") return;
    state.usersSearch = event.target.value;
    if (state.searchTimer) clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(
      () => loadUsers({ reset: true }),
      SEARCH_DEBOUNCE_MS,
    );
  });
  bindPeriodInput("period-start", "periodStart");
  bindPeriodInput("period-end", "periodEnd");

  document.addEventListener("click", async (event) => {
    const navigation = event.target.closest("[data-mobile-view]");
    if (navigation) {
      state.activeMobileView = navigation.dataset.mobileView;
      renderNavigation();
      return;
    }

    const action = event.target.closest("[data-action]");
    if (action) {
      switch (action.dataset.action) {
        case "toggle-preview": {
          const previewDialog = document.getElementById("preview-dialog");
          if (!previewDialog.open) {
            const backButton = telegramWebApp()?.BackButton;
            const closePreview = () => { if (previewDialog.open) previewDialog.close(); };
            previewDialog.addEventListener("close", () => {
              state.previewOpen = false;
              backButton?.offClick?.(closePreview);
              backButton?.hide?.();
              renderApp();
            }, { once: true });
            state.previewOpen = true;
            renderApp();
            previewDialog.showModal();
            backButton?.onClick?.(closePreview);
            backButton?.show?.();
          }
          return;
        }
        case "close-preview":
          document.getElementById("preview-dialog")?.close();
          return;
        case "clear-payout":
          clearPayoutSelection();
          return;
        case "open-history":
          state.activeMobileView = "payouts";
          renderNavigation();
          return;
        case "create-payout":
          await createPayout();
          return;
        case "go-selection":
          if (goSelectionInFlight) return;
          {
            const payoutId = Number(action.dataset.payoutId);
            if (!Number.isInteger(payoutId) || payoutId <= 0) {
              setToast("Не удалось определить созданную выплату.", "error");
              renderApp();
              return;
            }
            goSelectionInFlight = true;
            try {
              if (Number(state.selectedPayoutId) !== payoutId) {
                const selected = await selectPayout(payoutId, {
                  requireSuccessfulLoad: true,
                });
                if (!selected) {
                  setToast(
                    `Не удалось загрузить выплату #${payoutId}. Вы остались в текущем разделе.`,
                    "error",
                  );
                  renderApp();
                  return;
                }
              }
              state.activeMobileView = "users";
              renderNavigation();
            } finally {
              goSelectionInFlight = false;
            }
          }
          return;
        case "send-payout": {
          const payout = state.selectedPayoutDetail?.payout;
          const recipientIds = [...state.selectedUsers];
          const payoutId = Number(state.selectedPayoutId);
          const count = recipientIds.length;
          if (
            !canUseApi() ||
            payout?.status !== "draft" ||
            !count ||
            state.loading.sendPayout
          )
            return;
          showConfirmation({
            title: "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c \u0440\u0430\u0441\u0441\u044b\u043b\u043a\u0443",
            content: `\u0412\u044b\u043f\u043b\u0430\u0442\u0430: #${payout.id}\n\u041f\u0435\u0440\u0438\u043e\u0434: ${payoutPeriodLabel(payout)}\n\u041f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u0435\u0439: ${count}`,
            hideClose: true,
            confirmLabel: `\u0420\u0430\u0437\u043e\u0441\u043b\u0430\u0442\u044c ${count} \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f\u043c`,
            onConfirm: () => {
              const current = state.selectedPayoutDetail?.payout;
              const stillSelected = [...state.selectedUsers];
              if (Number(state.selectedPayoutId) !== payoutId || Number(payout.id) !== payoutId || current?.status !== "draft" || state.loading.sendPayout || stillSelected.length !== recipientIds.length || recipientIds.some((id) => !state.selectedUsers.has(id))) return;
              return sendPayout(recipientIds);
            },
          });
          return;
        }
        case "clear-selection":
          state.selectedUsers.clear();
          state.selectedUsersByPayout.set(
            Number(state.selectedPayoutId),
            new Set(),
          );
          renderUsers();
          document.querySelector('[data-action="send-payout"]').disabled = true;
          document.getElementById("selection-bar").classList.add("hidden");
          return;
        case "load-more-users":
          await loadUsers({ reset: false });
          return;
        case "dismiss-toast":
          clearNotification();
          renderApp();
          return;
        case "mark-paid": {
          const recipient = state.recipients.find(
            (item) => Number(item.id) === Number(action.dataset.recipientId),
          );
          const payout = state.selectedPayoutDetail?.payout;
          if (
            !recipient || !payout || state.loading.markPaid ||
            recipient.status !== "payment_received" ||
            ["draft", "sending", "completed", "cancelled"].includes(payout.status)
          ) return;
          const confirmedPayoutId = Number(payout.id);
          showConfirmation({
            title: "Подтвердить выплату?",
            content: `${recipient.full_name}\nВыплата #${payout.id} · ${payoutPeriodLabel(payout)}`,
            confirmLabel: "Подтвердить оплату",
            focusFallback: `[data-expand-recipient="${Number(recipient.id)}"]`,
            onConfirm: () => markPaid(
              Number(recipient.id),
              Number(recipient.payment_snapshot_version || 0),
              confirmedPayoutId,
            ),
          });
          return;
        }
        default:
          break;
      }
    }

    if (event.target.closest("[data-dialog-cancel]")) return;
    const payoutButton = event.target.closest("[data-payout-id]");
    if (payoutButton) {
      await selectPayout(Number(payoutButton.dataset.payoutId));
      renderApp();
      return;
    }
    if (event.target.closest("input[data-user-id]")) return;

    const filter = event.target.closest("[data-filter]");
    if (filter) {
      state.recipientFilter = filter.dataset.filter;
      renderRecipients();
    }
  });

  document.addEventListener("change", (event) => {
    const checkbox = event.target.closest?.("input[data-user-id]");
    if (!checkbox) return;
    const id = Number(checkbox.dataset.userId);
    if (
      !canUseApi() ||
      state.selectedPayoutDetail?.payout?.status !== "draft" ||
      state.loading.sendPayout
    ) {
      checkbox.checked = false;
      checkbox.closest(".user-row")?.classList.remove("selected");
      return;
    }
    checkbox.checked
      ? state.selectedUsers.add(id)
      : state.selectedUsers.delete(id);
    state.selectedUsersByPayout.set(
      Number(state.selectedPayoutId),
      new Set(state.selectedUsers),
    );
    checkbox
      .closest(".user-row")
      ?.classList.toggle("selected", checkbox.checked);
    const count = state.selectedUsers.size;
    document.getElementById("selected-count").textContent =
      `${count} ${plural(count, "получатель", "получателя", "получателей")}`;
    document
      .getElementById("selection-bar")
      .classList.toggle("hidden", !count || state.activeMobileView !== "users");
    document.querySelector('[data-action="send-payout"]').disabled =
      !count || state.loading.sendPayout;
  });

  document.addEventListener("submit", (event) => {
    if (!event.target.matches('[data-action-form="create-payout"]')) return;
    event.preventDefault();
    void createPayout();
  });
}

export async function bootstrap() {
  setThemeVariables();
  refreshTelegramAuthState();
  const webApp = telegramWebApp();
  if (webApp) {
    webApp.ready();
    webApp.expand();
  }
  state.activeMobileView = "composer";
  const defaultMessage =
    "Всем привет!\n" +
    "Выплачиваем зарплату за работу в период {period_label}.\n\n" +
    "Для получения выплаты проверьте или заполните платёжные данные.\n\n" +
    "Если перевод на ваши данные:\n" +
    "укажите фамилию и имя, номер телефона для перевода / СБП, банк.\n\n" +
    "Если перевод на чужие данные:\n" +
    "укажите вашу фамилию и имя, имя владельца, номер телефона владельца, банк.\n\n" +
    "После сохранения бот должен ответить: «Ваше сообщение сохранено».";
  const marker = "{period_label}";
  const markerIndex = defaultMessage.indexOf(marker);
  state.composer.messageBefore = defaultMessage.slice(0, markerIndex);
  state.composer.messageAfter = defaultMessage.slice(
    markerIndex + marker.length,
  );
  document.getElementById("message-before").value =
    state.composer.messageBefore;
  document.getElementById("message-after").value = state.composer.messageAfter;
  bindMessageEditors();
  bindEvents();
  renderApp();
  bindTelegramViewportEvents(syncViewportVariables);
  window.addEventListener("pagehide", stopAdminEvents);
  window.addEventListener("beforeunload", stopAdminEvents);
  if (!canUseApi()) return;
  void startAdminEvents();
  await Promise.allSettled([loadUsers({ reset: true }), loadPayouts()]);
}
