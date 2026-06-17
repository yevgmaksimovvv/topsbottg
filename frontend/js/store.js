import { COMPOSER_IDS, MOBILE_QUERY, USERS_LIMIT } from "./constants.js";

export const state = {
  isTelegramEnvironment: Boolean(window.Telegram?.WebApp),
  initData: "",
  authStatus: window.Telegram?.WebApp ? "telegram_missing_init_data" : "browser",
  activeMobileView: "users",
  users: [],
  payouts: [],
  recipients: [],
  selectedUsers: new Set(),
  selectedPayoutId: null,
  selectedPayoutDetail: null,
  composer: {
    periodStartDay: "",
    periodStartMonth: "",
    periodEndDay: "",
    periodEndMonth: "",
    messageTemplate: "",
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
  toast: null,
  pollingTimer: null,
  usersRequestId: 0,
  payoutRequestId: 0,
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
  state.error = message || null;
}

export function clearError() {
  state.error = null;
}

export function setToast(message) {
  state.toast = message || null;
}

export function setMobileView(view) {
  state.activeMobileView = view;
}

export function getComposerElements() {
  const side = MOBILE_QUERY.matches ? "mobile" : "desktop";
  const ids = COMPOSER_IDS[side];
  return {
    side,
    periodStartDay: $(ids.periodStartDay),
    periodStartMonth: $(ids.periodStartMonth),
    periodEndDay: $(ids.periodEndDay),
    periodEndMonth: $(ids.periodEndMonth),
    messageTemplate: $(ids.messageTemplate),
    validation: $(ids.validation),
    preview: $(ids.preview),
  };
}

export function getAllComposerElementPairs() {
  return Object.values(COMPOSER_IDS).map((ids) => ({
    periodStartDay: $(ids.periodStartDay),
    periodStartMonth: $(ids.periodStartMonth),
    periodEndDay: $(ids.periodEndDay),
    periodEndMonth: $(ids.periodEndMonth),
    messageTemplate: $(ids.messageTemplate),
    validation: $(ids.validation),
    preview: $(ids.preview),
  }));
}

export function syncFilterInputs() {
  ["desktop-search", "mobile-search"].forEach((id) => {
    const input = $(id);
    if (input) input.value = state.usersSearch;
  });
}

export function syncComposerFields() {
  getAllComposerElementPairs().forEach((fields) => {
    if (fields.periodStartDay) fields.periodStartDay.value = state.composer.periodStartDay;
    if (fields.periodStartMonth) fields.periodStartMonth.value = state.composer.periodStartMonth;
    if (fields.periodEndDay) fields.periodEndDay.value = state.composer.periodEndDay;
    if (fields.periodEndMonth) fields.periodEndMonth.value = state.composer.periodEndMonth;
    if (fields.messageTemplate) fields.messageTemplate.value = state.composer.messageTemplate;
    if (fields.validation) {
      fields.validation.textContent = "";
      fields.validation.classList.add("hidden");
    }
  });
}
