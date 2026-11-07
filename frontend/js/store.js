import { AUTO_HIDE_TOAST_MS, COMPOSER_IDS, MOBILE_QUERY, USERS_LIMIT } from "./constants.js";
import { composerPeriodLabel } from "./message-limit.mjs";

export const state = {
  isTelegramEnvironment: Boolean(window.Telegram?.WebApp),
  initData: "",
  authStatus: window.Telegram?.WebApp ? "telegram_missing_init_data" : "browser",
  activeMobileView: "composer",
  users: [],
  payouts: [],
  recipients: [],
  selectedUsers: new Set(),
  selectedPayoutId: null,
  selectedPayoutDetail: null,
  composer: {
    periodStart: "",
    periodEnd: "",
    messageBefore: "",
    messageAfter: "",
  },
  usersLimit: USERS_LIMIT,
  usersOffset: 0,
  usersHasMore: false,
  usersSearch: "",
  loading: {
    users: false,
    payouts: false,
    recipients: false,
    createPayout: false,
    sendPayout: false,
    markPaid: false,
  },
  loadingRecipientId: null,
  error: null,
  notification: null,
  usersRequestId: 0,
  payoutRequestId: 0,
  payoutsRequestId: 0,
  selectedPayoutRequestId: 0,
  toastTimer: null,
  searchTimer: null,
};

export const $ = (id) => document.getElementById(id);

function padPeriodPart(value) {
  return String(value).padStart(2, "0");
}

export function formatPayoutPeriodLabel(startDay, startMonth, endDay, endMonth) {
  return `${padPeriodPart(startDay)}.${padPeriodPart(startMonth)} — ${padPeriodPart(endDay)}.${padPeriodPart(endMonth)}`;
}

export function payoutPeriodLabel(payout) {
  if (!payout) return "";
  if (payout.period_label) return payout.period_label;
  if (
    payout.period_start_day &&
    payout.period_start_month &&
    payout.period_end_day &&
    payout.period_end_month
  ) {
    return formatPayoutPeriodLabel(
      payout.period_start_day,
      payout.period_start_month,
      payout.period_end_day,
      payout.period_end_month
    );
  }
  return "";
}

function readTelegramWebApp() {
  return window.Telegram?.WebApp || null;
}

export function refreshTelegramAuthState() {
  const webApp = readTelegramWebApp();
  state.isTelegramEnvironment = Boolean(webApp);
  state.initData = webApp?.initData || "";
  if (!webApp) {
    state.authStatus = "browser";
    return;
  }
  state.authStatus = state.initData ? "ready" : "telegram_missing_init_data";
}

export function canUseApi() {
  return Boolean(state.initData);
}

export function setLoading(key, value) {
  state.loading[key] = value;
}

export function setError(message) {
  const text = String(message || "").trim();
  state.error = text || null;
  setToast(text, "error");
}

export function clearError() {
  state.error = null;
  clearNotification();
}

function hideNotificationDom() {
  const toast = $("toast");
  if (toast) {
    toast.textContent = "";
    toast.className = "toast hidden";
  }
}

function clearNotificationTimer() {
  if (state.toastTimer) {
    clearTimeout(state.toastTimer);
    state.toastTimer = null;
  }
}

export function clearNotification() {
  clearNotificationTimer();
  state.notification = null;
  hideNotificationDom();
}

function notificationDuration(kind) {
  if (kind === "warning" || kind === "error") return 5000;
  return AUTO_HIDE_TOAST_MS;
}

export function setToast(message, kind = "info") {
  const text = String(message || "").trim();
  clearNotificationTimer();
  state.notification = text ? { kind, message: text } : null;
  if (!text) {
    hideNotificationDom();
    return;
  }
  state.toastTimer = window.setTimeout(() => {
    state.notification = null;
    state.toastTimer = null;
    hideNotificationDom();
  }, notificationDuration(kind));
}

export function setMobileView(view) {
  state.activeMobileView = view;
}

export function getComposerElements() {
  const side = MOBILE_QUERY.matches ? "mobile" : "desktop";
  const ids = COMPOSER_IDS[side];
  return {
    side,
    periodStart: $(ids.periodStart), periodEnd: $(ids.periodEnd),
    messageBefore: $(ids.messageBefore), messageAfter: $(ids.messageAfter),
    periodLabel: document.querySelector(`#${side}-protected-period`),
    messageCount: $(ids.messageCount), validation: $(ids.validation), preview: $(ids.preview),
  };
}

export function getAllComposerElementPairs() {
  return Object.values(COMPOSER_IDS).map((ids) => ({
    periodStart: $(ids.periodStart), periodEnd: $(ids.periodEnd),
    messageBefore: $(ids.messageBefore), messageAfter: $(ids.messageAfter),
    periodLabel: document.querySelector(`#${ids.messageBefore.split("-")[0]}-protected-period`),
    messageCount: $(ids.messageCount), validation: $(ids.validation), preview: $(ids.preview),
  }));
}

export function syncFilterInputs() {
  ["desktop-search", "mobile-search"].forEach((id) => {
    const input = $(id);
    if (input) input.value = state.usersSearch;
  });
}

export function syncComposerFields() {
  const periodLabel = composerPeriodLabel(state.composer);
  getAllComposerElementPairs().forEach((fields) => {
    for (const key of ["periodStart", "periodEnd", "messageBefore", "messageAfter"]) {
      if (!fields[key]) continue;
      if (key.startsWith("period")) {
        if (fields[key].value !== state.composer[key]) fields[key].value = state.composer[key];
      } else if (fields[key].value !== state.composer[key]) {
        fields[key].value = state.composer[key];
      }
    }
    if (fields.periodLabel && fields.periodLabel.textContent !== periodLabel) fields.periodLabel.textContent = periodLabel;
    if (fields.validation && !fields.validation.textContent) fields.validation.classList.add("hidden");
  });
}
