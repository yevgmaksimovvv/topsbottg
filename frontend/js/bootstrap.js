import { SEARCH_DEBOUNCE_MS } from "./constants.js";
import { renderApp } from "./render-app.js";
import { bindTelegramViewportEvents, setThemeVariables, syncViewportVariables, telegramWebApp } from "./telegram.js";
import {
  clearNotification,
  canUseApi,
  refreshTelegramAuthState,
  setError,
  state,
  setMobileView,
  syncComposerFields,
  syncFilterInputs,
} from "./store.js";
import {
  loadPayouts,
  loadUsers,
  markPaid,
  scheduleAdminStateRefresh,
  selectPayout,
  startAdminEvents,
  stopAdminEvents,
} from "./api.js";
import { createPayout, sendPayout } from "./render-payouts.js";

let adminRecoveryTimer = null;
let adminRecoveryReason = null;

function syncTelegramSafeAreaVariables() {
  const webApp = telegramWebApp();
  const root = document.documentElement.style;
  const safeAreaBottom = Math.max(0, Math.round(webApp?.safeAreaInset?.bottom || 0));
  const contentSafeAreaBottom = Math.max(0, Math.round(webApp?.contentSafeAreaInset?.bottom || 0));
  root.setProperty("--tg-safe-area-inset-bottom", `${safeAreaBottom}px`);
  root.setProperty("--tg-content-safe-area-inset-bottom", `${contentSafeAreaBottom}px`);
}

function syncLayoutMetrics() {
  syncViewportVariables();
  syncTelegramSafeAreaVariables();
}

function scheduleAdminRecoveryRefresh(reason = "visibilitychange") {
  if (!canUseApi()) return;
  adminRecoveryReason = reason;
  if (adminRecoveryTimer) {
    clearTimeout(adminRecoveryTimer);
  }
  adminRecoveryTimer = window.setTimeout(() => {
    adminRecoveryTimer = null;
    const currentReason = adminRecoveryReason;
    adminRecoveryReason = null;
    console.info("[topsbottg] recovery refresh", {
      build: window.__TOPSBOTTG_FRONTEND_BUILD__ || null,
      reason: currentReason,
      visibilityState: document.visibilityState,
    });
    void (async () => {
      await startAdminEvents();
      await scheduleAdminStateRefresh({ selected: true, payouts: true, silent: true });
    })();
  }, 700);
}

function bindComposerInputs() {
  const ids = [
    ["desktop-period-start-day", "periodStartDay"],
    ["desktop-period-start-month", "periodStartMonth"],
    ["desktop-period-end-day", "periodEndDay"],
    ["desktop-period-end-month", "periodEndMonth"],
    ["desktop-message-template", "messageTemplate"],
    ["mobile-period-start-day", "periodStartDay"],
    ["mobile-period-start-month", "periodStartMonth"],
    ["mobile-period-end-day", "periodEndDay"],
    ["mobile-period-end-month", "periodEndMonth"],
    ["mobile-message-template", "messageTemplate"],
  ];

  ids.forEach(([id, key]) => {
    document.getElementById(id)?.addEventListener("input", (event) => {
      state.composer[key] = event.target.value;
      syncComposerFields();
      renderApp();
    });
  });
}

function bindFilterInputs() {
  const searchIds = ["desktop-search", "mobile-search"];

  searchIds.forEach((id) => {
    document.getElementById(id)?.addEventListener("input", (event) => {
      state.usersSearch = event.target.value;
      syncFilterInputs();
      if (state.searchTimer) clearTimeout(state.searchTimer);
      state.searchTimer = window.setTimeout(() => loadUsers({ reset: true }), SEARCH_DEBOUNCE_MS);
      renderApp();
    });
  });
}

function getActionElement(event) {
  const target = event.target;
  if (!(target instanceof Element)) return null;
  return target.closest("[data-action]");
}

function isDisabledAction(action) {
  return Boolean(
    action.disabled || action.getAttribute("aria-disabled") === "true" || action.classList.contains("disabled")
  );
}

function bindDelegatedEvents() {
  document.addEventListener("click", async (event) => {
    const mobileView = event.target.closest("[data-mobile-view]");
    const userCheckbox = event.target.closest('input[type="checkbox"][data-user-id]');
    const payoutButton = event.target.closest("[data-payout-id]");
    const action = getActionElement(event);

    if (mobileView) {
      setMobileView(mobileView.dataset.mobileView);
      renderApp();
      return;
    }

    if (userCheckbox) return;

    if (payoutButton) {
      event.preventDefault();
      await selectPayout(Number(payoutButton.dataset.payoutId));
      renderApp();
      return;
    }

    if (!action) return;

    const name = action.dataset.action;
    if (!name) return;
    event.preventDefault();
    if (isDisabledAction(action)) return;

    try {
      switch (name) {
        case "dismiss-toast":
          clearNotification();
          renderApp();
          return;
        case "create-payout":
          await createPayout();
          return;
        case "send-payout":
          await sendPayout();
          return;
        case "mark-paid": {
          const recipientId = action.dataset.recipientId;
          if (!recipientId) return;
          await markPaid(Number(recipientId));
          return;
        }
        case "load-more-users":
          await loadUsers({ reset: false });
          return;
        case "clear-selection":
          state.selectedUsers.clear();
          renderApp();
          return;
        default:
          return;
      }
    } catch (error) {
      console.error("[topsbottg] action failed", {
        action: name,
        error,
      });
      setError(error?.message || "Действие не выполнено.");
      renderApp();
    }
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.matches('[data-action-form="create-payout"]')) return;
    event.preventDefault();
    await createPayout();
  });

  document.addEventListener("change", (event) => {
    const target = event.target;
    const checkbox = target?.closest?.('input[type="checkbox"][data-user-id]');
    if (!checkbox) return;
    const id = Number(checkbox.dataset.userId);
    if (checkbox.checked) state.selectedUsers.add(id);
    else state.selectedUsers.delete(id);
    renderApp();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") renderApp();
  });
}

export async function bootstrap() {
  setThemeVariables();
  console.info("[topsbottg] startup", {
    build: window.__TOPSBOTTG_FRONTEND_BUILD__ || null,
    userAgent: navigator.userAgent,
    telegramWebApp: Boolean(window.Telegram?.WebApp),
    visibilityState: document.visibilityState,
  });
  const webApp = telegramWebApp();
  if (webApp) {
    webApp.ready();
    webApp.expand();
  }
  refreshTelegramAuthState();
  setMobileView("composer");

  state.composer.messageTemplate =
    "Всем привет!\n" +
    "Выплачиваем ЗАРПЛАТУ за работу в период {period_label}.\n\n" +
    "Для получения выплаты проверьте или заполните платежные данные.\n\n" +
    "Если перевод на ваши данные:\n" +
    "укажите фамилию и имя, номер телефона для перевода / СБП, банк.\n\n" +
    "Если перевод на чужие данные:\n" +
    "укажите ваши фамилию и имя, имя владельца, номер телефона владельца, банк.\n\n" +
    "После сохранения бот должен ответить: «Ваше сообщение сохранено».";

  syncFilterInputs();
  syncComposerFields();
  bindComposerInputs();
  bindFilterInputs();
  bindDelegatedEvents();
  renderApp();
  syncLayoutMetrics();
  bindTelegramViewportEvents(syncLayoutMetrics);
  window.addEventListener("pagehide", stopAdminEvents);
  window.addEventListener("beforeunload", stopAdminEvents);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      scheduleAdminRecoveryRefresh("visibilitychange");
    }
  });
  window.addEventListener("focus", () => scheduleAdminRecoveryRefresh("focus"));
  window.addEventListener("pageshow", () => scheduleAdminRecoveryRefresh("pageshow"));

  if (!state.initData) return;
  void startAdminEvents();
  await Promise.allSettled([loadUsers({ reset: true }), loadPayouts()]);
}
