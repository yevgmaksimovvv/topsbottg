import { AUTO_HIDE_TOAST_MS, COMPOSER_IDS, USERS_LIMIT } from "./constants.js";
import { composerPeriodLabel } from "./message-limit.js";
import { formatPayoutPeriodLabel } from "./utils.js";

export { formatPayoutPeriodLabel } from "./utils.js";

export const state = {
  isTelegramEnvironment: Boolean(window.Telegram?.WebApp),
  initData: "",
  authStatus: window.Telegram?.WebApp
    ? "telegram_missing_init_data"
    : "browser",
  activeMobileView: "composer",
  users: [],
  payouts: [],
  recipients: [],
  usersLoadError: null,
  payoutsLoadError: null,
  recipientsLoadError: null,
  selectedUsers: new Set(),
  selectedUsersByPayout: new Map(),
  selectionGenerationByPayout: new Map(),
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
  recipientFilter: "all",
  expandedRecipients: new Set(),
  recipientOperationError: {},
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
  previewOpen: false,
  dialogBusy: false,
};

export const $ = (id) => document.getElementById(id);

export function payoutPeriodLabel(payout) {
  if (!payout) return "";
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
      payout.period_end_month,
    );
  }
  return payout.period_label || "";
}

export function refreshTelegramAuthState() {
  const webApp = window.Telegram?.WebApp;
  state.isTelegramEnvironment = Boolean(webApp);
  state.initData = webApp?.initData || "";
  state.authStatus = !webApp
    ? "browser"
    : state.initData
      ? "ready"
      : "telegram_missing_init_data";
}

export const canUseApi = () => Boolean(state.initData);
export function setLoading(key, value) {
  state.loading[key] = value;
}

export function setError(message) {
  state.error = String(message || "").trim() || null;
  setToast(state.error, "error");
}

export function clearError() {
  state.error = null;
  clearNotification();
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
  const toast = $("toast");
  if (toast) {
    toast.textContent = "";
    toast.className = "toast hidden";
  }
}

export function setToast(message, kind = "info") {
  clearNotificationTimer();
  const text = String(message || "").trim();
  state.notification = text ? { kind, message: text } : null;
  if (text) {
    state.toastTimer = window.setTimeout(
      clearNotification,
      kind === "warning" || kind === "error" ? 5000 : AUTO_HIDE_TOAST_MS,
    );
  }
}

export function setMobileView(view) {
  state.activeMobileView = view;
}

export function syncFilterInputs() {
  const input = $("users-search");
  if (input && input.value !== state.usersSearch)
    input.value = state.usersSearch;
}

export function syncComposerFields() {
  for (const [key, id] of Object.entries(COMPOSER_IDS)) {
    if (["messageCount", "validation", "preview"].includes(key)) continue;
    const field = $(id);
    if (field && field.value !== state.composer[key])
      field.value = state.composer[key];
  }
  const period = $("protected-period");
  if (period) period.textContent = composerPeriodLabel(state.composer);
}
