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
    title: "",
    periodFrom: "",
    periodTo: "",
    messageTemplate: "",
  },
  usersLimit: USERS_LIMIT,
  usersOffset: 0,
  usersHasMore: false,
  usersSearch: "",
  usersHasPayment: "",
  usersIsActive: "",
  loading: {
    users: false,
    payouts: false,
    recipients: false,
    createPayout: false,
    attachSelected: false,
    sendPayout: false,
    exportCsv: false,
    markPaid: false,
    revealPaymentDetails: false,
  },
  loadingUserId: null,
  loadingRecipientId: null,
  error: null,
  toast: null,
  pollingTimer: null,
  usersRequestId: 0,
  payoutRequestId: 0,
  modalPaymentDetails: null,
  toastTimer: null,
  searchTimer: null,
};

export const $ = (id) => document.getElementById(id);

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
    title: $(ids.title),
    periodFrom: $(ids.periodFrom),
    periodTo: $(ids.periodTo),
    messageTemplate: $(ids.messageTemplate),
    validation: $(ids.validation),
    preview: $(ids.preview),
  };
}

export function getAllComposerElementPairs() {
  return Object.values(COMPOSER_IDS).map((ids) => ({
    title: $(ids.title),
    periodFrom: $(ids.periodFrom),
    periodTo: $(ids.periodTo),
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
  ["desktop-has-payment", "mobile-has-payment"].forEach((id) => {
    const select = $(id);
    if (select) select.value = state.usersHasPayment;
  });
  ["desktop-is-active", "mobile-is-active"].forEach((id) => {
    const select = $(id);
    if (select) select.value = state.usersIsActive;
  });
}

export function syncComposerFields() {
  getAllComposerElementPairs().forEach((fields) => {
    if (fields.title) fields.title.value = state.composer.title;
    if (fields.periodFrom) fields.periodFrom.value = state.composer.periodFrom;
    if (fields.periodTo) fields.periodTo.value = state.composer.periodTo;
    if (fields.messageTemplate) fields.messageTemplate.value = state.composer.messageTemplate;
    if (fields.validation) {
      fields.validation.textContent = "";
      fields.validation.classList.add("hidden");
    }
  });
}
