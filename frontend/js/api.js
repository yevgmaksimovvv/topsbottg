import { API_TIMEOUT_MS } from "./constants.js";
import { state, canUseApi, clearError, setError, setLoading, setToast } from "./store.js";
import { renderApp } from "./render-app.js";
import {
  createPayout as renderCreatePayout,
  sendPayout as renderSendPayout,
} from "./render-payouts.js";

const TEMPORARY_BACKEND_ERROR = "Backend временно недоступен. Проверьте сервер приложения.";
const AUTH_REQUIRED_ERROR = "Требуется вход через Telegram.";
const FORBIDDEN_ERROR = "Недостаточно прав администратора.";

let adminEventsSource = null;
let adminEventsStartPromise = null;
let adminEventsRecoveryUsed = false;
let adminEventsWarningShown = false;

function isJsonResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json");
}

function isHtmlText(text, contentType = "") {
  const trimmed = text.trimStart();
  return contentType.includes("text/html") || trimmed.startsWith("<!doctype html") || trimmed.startsWith("<html");
}

function safeDetailMessage(detail) {
  if (typeof detail === "string") return detail.trim();
  if (Array.isArray(detail)) {
    return detail
      .map((item) => (typeof item === "string" ? item.trim() : ""))
      .filter(Boolean)
      .join(" ");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return "";
}

async function readErrorMessage(response) {
  const contentType = response.headers.get("content-type") || "";
  if (response.status === 502 || response.status === 503 || response.status === 504) {
    return TEMPORARY_BACKEND_ERROR;
  }
  if (response.status === 401) return AUTH_REQUIRED_ERROR;
  if (response.status === 403) return FORBIDDEN_ERROR;
  if (isJsonResponse(response)) {
    try {
      const data = await response.json();
      const detail = safeDetailMessage(data?.detail);
      if (detail) return detail;
      return "Запрос не выполнен.";
    } catch {
      return "Запрос не выполнен.";
    }
  }
  try {
    const text = await response.text();
    if (isHtmlText(text, contentType)) {
      return response.status >= 500 ? TEMPORARY_BACKEND_ERROR : "Сервер вернул некорректный ответ.";
    }
    const trimmed = text.trim();
    return trimmed || "Запрос не выполнен.";
  } catch {
    return "Запрос не выполнен.";
  }
}

async function api(path, options = {}) {
  if (!canUseApi()) {
    throw new Error("Нет доступа к данным");
  }
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort("timeout"), API_TIMEOUT_MS);
  try {
    const response = await fetch(`/api${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        ...(options.headers || {}),
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": state.initData,
      },
    });
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    if (response.status === 204) return null;
    if (isJsonResponse(response)) return response.json();
    return response.text();
  } catch (error) {
    if (error?.name === "AbortError" || error === "timeout") {
      throw new Error("Запрос превысил время ожидания.");
    }
    throw new Error(error?.message || "Не удалось выполнить запрос.");
  } finally {
    clearTimeout(timeoutId);
  }
}

function setLoadingAndRender(key, value) {
  setLoading(key, value);
  renderApp();
}

function setErrorAndRender(message) {
  setError(message);
  renderApp();
}

function setToastAndRender(message) {
  setToast(message, "success");
  renderApp();
}

function logLoadError(scope, error) {
  console.error(`[topsbottg] ${scope}:`, error?.message || error);
}

function parseEventPayload(data) {
  try {
    return JSON.parse(data || "{}");
  } catch {
    return {};
  }
}

async function loadUsers({ reset = true, silent = false } = {}) {
  if (!canUseApi()) return;
  const requestId = ++state.usersRequestId;
  if (reset) {
    state.usersOffset = 0;
    state.users = [];
    state.usersHasMore = false;
  }
  if (!silent) {
    setLoadingAndRender("users", true);
    clearError();
    renderApp();
  }
  try {
    const params = new URLSearchParams();
    params.set("limit", String(state.usersLimit));
    params.set("offset", String(state.usersOffset));
    if (state.usersSearch.trim()) params.set("search", state.usersSearch.trim());
    const page = await api(`/admin/users?${params.toString()}`);
    if (requestId !== state.usersRequestId) return;
    state.users = reset ? page.items : [...state.users, ...page.items];
    state.usersHasMore = page.has_more;
    state.usersOffset = page.offset + page.items.length;
    if (silent) renderApp();
  } catch (error) {
    if (requestId === state.usersRequestId) {
      logLoadError("loadUsers", error);
      if (!silent) setErrorAndRender(error.message || "Не удалось загрузить пользователей");
    }
  } finally {
    if (requestId === state.usersRequestId) {
      if (!silent) setLoadingAndRender("users", false);
    }
  }
}

async function loadPayouts({ silent = false } = {}) {
  if (!canUseApi()) return;
  const requestId = ++state.payoutsRequestId;
  if (!silent) {
    setLoadingAndRender("payouts", true);
    clearError();
    renderApp();
  }
  try {
    const payouts = await api("/admin/payouts");
    if (requestId !== state.payoutsRequestId) return;
    state.payouts = payouts;
    if (silent) renderApp();
  } catch (error) {
    if (requestId === state.payoutsRequestId) {
      logLoadError("loadPayouts", error);
      if (!silent) setErrorAndRender(error.message || "Не удалось загрузить выплаты");
    }
  } finally {
    if (requestId === state.payoutsRequestId) {
      if (!silent) setLoadingAndRender("payouts", false);
    }
  }
}

async function refreshSelectedPayout({ silent = false } = {}) {
  const hasInitData = canUseApi();
  console.info("[admin-events] refresh-start", {
    selectedPayoutId: state.selectedPayoutId,
    silent,
    hasInitData,
  });
  if (!state.selectedPayoutId) {
    console.info("[admin-events] refresh-skip", {
      selectedPayoutId: state.selectedPayoutId,
      silent,
      hasInitData,
      reason: "missing_selectedPayoutId",
    });
    return;
  }
  if (!hasInitData) {
    console.info("[admin-events] refresh-skip", {
      selectedPayoutId: state.selectedPayoutId,
      silent,
      hasInitData,
      reason: "missing_initData",
    });
    return;
  }
  const requestId = ++state.selectedPayoutRequestId;
  if (!silent) setLoadingAndRender("recipients", true);
  try {
    const detail = await api(`/admin/payouts/${state.selectedPayoutId}`);
    const recipients = await api(`/admin/payouts/${state.selectedPayoutId}/recipients`);
    if (requestId !== state.selectedPayoutRequestId) return;
    state.selectedPayoutDetail = detail;
    state.recipients = recipients;
    console.info("[admin-events] refresh-success", {
      selectedPayoutId: state.selectedPayoutId,
      recipientsCount: Array.isArray(recipients) ? recipients.length : null,
      payoutStatus: detail?.payout?.status ?? null,
    });
    if (silent) renderApp();
  } catch (error) {
    logLoadError("refreshSelectedPayout", error);
    console.warn("[admin-events] refresh-error", {
      selectedPayoutId: state.selectedPayoutId,
      silent,
      errorName: error?.name || "Error",
      errorMessage: error?.message || String(error || ""),
    });
    if (!silent) setErrorAndRender(error.message || "Не удалось обновить выплату");
  } finally {
    if (!silent) setLoadingAndRender("recipients", false);
  }
}

export async function createEventsToken() {
  return api("/admin/events-token", { method: "POST" });
}

export function stopAdminEvents({ resetRecovery = true } = {}) {
  console.info("[admin-events] stop", {
    readyState: adminEventsSource?.readyState ?? null,
    recoverAttempted: adminEventsRecoveryUsed,
  });
  if (adminEventsSource) {
    adminEventsSource.close();
    adminEventsSource = null;
  }
  if (resetRecovery) adminEventsRecoveryUsed = false;
  adminEventsStartPromise = null;
}

export function handleAdminEvent(type, payload = {}) {
  const payloadPayoutId = payload?.payout_id ?? null;
  const selectedPayoutId = state.selectedPayoutId;
  const idsMatch = payloadPayoutId != null && Number(payloadPayoutId) === Number(selectedPayoutId);
  console.info("[admin-events] handle", {
    eventType: type,
    payloadPayoutId,
    selectedPayoutId,
    idsMatch,
    eventSourceReadyState: adminEventsSource?.readyState ?? null,
  });
  if (type === "ping") return;
  if (type === "users_changed") {
    if (state.initData) void loadUsers({ reset: true, silent: true });
    return;
  }
  if (type === "payouts_changed") {
    if (state.initData) void loadPayouts({ silent: true });
    return;
  }
  if (type === "payout_changed") {
    if (state.initData) {
      if (payload.payout_id && Number(payload.payout_id) === Number(state.selectedPayoutId)) {
        console.info("[admin-events] refresh-triggered", {
          eventType: type,
          payloadPayoutId,
          selectedPayoutId,
        });
        void (async () => {
          await refreshSelectedPayout({ silent: true });
          await loadPayouts({ silent: true });
        })();
      } else {
        void loadPayouts({ silent: true });
      }
    }
    return;
  }
  if (type === "payout_recipients_changed") {
    if (state.initData && payload.payout_id && Number(payload.payout_id) === Number(state.selectedPayoutId)) {
      console.info("[admin-events] refresh-triggered", {
        eventType: type,
        payloadPayoutId,
        selectedPayoutId,
      });
      void refreshSelectedPayout({ silent: true });
    }
  }
}

function handleAdminEventsError(source, retryTokenRefresh) {
  if (source !== adminEventsSource) return;
  if (!adminEventsRecoveryUsed) {
    adminEventsRecoveryUsed = true;
    stopAdminEvents({ resetRecovery: false });
    void startAdminEvents(retryTokenRefresh);
    return;
  }
  stopAdminEvents();
  if (!adminEventsWarningShown) {
    adminEventsWarningShown = true;
    setToast("Realtime обновление временно недоступно.", "warning");
    renderApp();
  }
}

export async function startAdminEvents(retryTokenRefresh = false) {
  console.info("[admin-events] start", {
    readyState: adminEventsSource?.readyState ?? null,
    recoverAttempted: retryTokenRefresh,
  });
  if (!state.initData || !canUseApi()) return null;
  if (adminEventsSource && adminEventsSource.readyState !== EventSource.CLOSED) return adminEventsSource;
  if (adminEventsStartPromise) return adminEventsStartPromise;
  adminEventsStartPromise = (async () => {
    try {
      const { token } = await createEventsToken();
      if (adminEventsSource && adminEventsSource.readyState !== EventSource.CLOSED) {
        return adminEventsSource;
      }
      const source = new EventSource(`/api/admin/events?token=${encodeURIComponent(token)}`);
      adminEventsSource = source;
      adminEventsWarningShown = false;
      source.addEventListener("open", () => {
        const recoverAttempted = adminEventsRecoveryUsed;
        adminEventsRecoveryUsed = false;
        console.info("[admin-events] open", {
          readyState: source.readyState,
          recoverAttempted,
        });
      });
      source.addEventListener("ping", () => handleAdminEvent("ping", {}));
      source.addEventListener("users_changed", (event) => handleAdminEvent("users_changed", parseEventPayload(event.data)));
      source.addEventListener("payouts_changed", (event) => handleAdminEvent("payouts_changed", parseEventPayload(event.data)));
      source.addEventListener("payout_changed", (event) => handleAdminEvent("payout_changed", parseEventPayload(event.data)));
      source.addEventListener("payout_recipients_changed", (event) =>
        handleAdminEvent("payout_recipients_changed", parseEventPayload(event.data))
      );
      source.onerror = () => {
        console.warn("[admin-events] error", {
          readyState: source.readyState,
          recoverAttempted: adminEventsRecoveryUsed,
        });
        handleAdminEventsError(source, true);
      };
      return source;
    } catch (error) {
      if (retryTokenRefresh && !adminEventsWarningShown) {
        adminEventsWarningShown = true;
        setToast("Realtime обновление временно недоступно.", "warning");
        renderApp();
      }
      stopAdminEvents();
      return null;
    } finally {
      adminEventsStartPromise = null;
    }
  })();
  return adminEventsStartPromise;
}

async function selectPayout(id) {
  if (!canUseApi()) return;
  const changed = state.selectedPayoutId !== id;
  if (changed) {
    state.selectedUsers.clear();
    state.selectedPayoutDetail = null;
    state.recipients = [];
  }
  state.selectedPayoutId = id;
  await refreshSelectedPayout();
  renderApp();
}

async function markPaid(recipientId) {
  if (!canUseApi() || !state.selectedPayoutId) return;
  const confirmed = window.confirm("Вы уверены, что выплата этому получателю выполнена?");
  if (!confirmed) return;
  setLoadingAndRender("markPaid", true);
  state.loadingRecipientId = recipientId;
  renderApp();
  try {
    await api(`/admin/payouts/${state.selectedPayoutId}/recipients/${recipientId}/mark-paid`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    setToastAndRender("Получатель отмечен как выплаченный.");
    await refreshSelectedPayout();
    await loadPayouts();
  } catch (error) {
    setErrorAndRender(error.message || "Не удалось обновить статус");
  } finally {
    state.loadingRecipientId = null;
    setLoadingAndRender("markPaid", false);
    renderApp();
  }
}

export {
  api,
  renderCreatePayout as createPayout,
  loadPayouts,
  loadUsers,
  markPaid,
  refreshSelectedPayout,
  selectPayout,
  renderSendPayout as sendPayout,
};
