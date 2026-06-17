import { SEARCH_DEBOUNCE_MS } from "./constants.js";
import { renderApp } from "./render-app.js";
import { bindTelegramViewportEvents, setThemeVariables, syncViewportVariables, telegramWebApp } from "./telegram.js";
import { clearNotification, canUseApi, refreshTelegramAuthState, state, setMobileView, syncComposerFields, syncFilterInputs } from "./store.js";
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

function bindDelegatedEvents() {
  document.addEventListener("click", async (event) => {
    const action = event.target.closest("[data-action]");
    const mobileView = event.target.closest("[data-mobile-view]");
    const userCheckbox = event.target.closest('input[type="checkbox"][data-user-id]');
    const payoutButton = event.target.closest("[data-payout-id]");

    if (mobileView) {
      setMobileView(mobileView.dataset.mobileView);
      renderApp();
      return;
    }

    if (userCheckbox) return;

    if (payoutButton) {
      await selectPayout(Number(payoutButton.dataset.payoutId));
      renderApp();
      return;
    }

    if (action?.dataset.action === "dismiss-toast") {
      clearNotification();
      renderApp();
      return;
    }

    if (action) {
      const { action: name, recipientId } = action.dataset;
      if (name === "load-more-users") {
        await loadUsers({ reset: false });
      } else if (name === "clear-selection") {
        state.selectedUsers.clear();
        renderApp();
      } else if (name === "create-payout") {
        return;
      } else if (name === "send-payout") {
        await sendPayout();
      } else if (name === "mark-paid") {
        await markPaid(Number(recipientId));
      }
      renderApp();
      return;
    }
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form?.matches?.('[data-action-form="create-payout"]')) return;
    event.preventDefault();
    await createPayout();
  });

  document.addEventListener("change", (event) => {
    const checkbox = event.target.closest('input[type="checkbox"][data-user-id]');
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
