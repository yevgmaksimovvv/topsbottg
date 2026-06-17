import { POLL_INTERVAL_MS } from "./constants.js";
import { state, canUseApi, clearError, setError, setLoading, setToast } from "./store.js";
import { renderApp } from "./render-app.js";
import {
  createPayout as renderCreatePayout,
  sendPayout as renderSendPayout,
} from "./render-payouts.js";

const TEMPORARY_BACKEND_ERROR = "Backend временно недоступен. Проверьте сервер приложения.";
const AUTH_REQUIRED_ERROR = "Требуется вход через Telegram.";
const FORBIDDEN_ERROR = "Недостаточно прав администратора.";

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
  const response = await fetch(`/api${path}`, {
    ...options,
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
  setToast(message);
  renderApp();
}

function stopPolling() {
  if (state.pollingTimer) {
    clearInterval(state.pollingTimer);
    state.pollingTimer = null;
  }
}

function startPollingSelectedPayout() {
  stopPolling();
  if (!state.selectedPayoutDetail || state.selectedPayoutDetail.payout.status !== "sending") return;
  state.pollingTimer = window.setInterval(() => {
    if (state.selectedPayoutId && !state.loading.recipients) {
      refreshSelectedPayout({ silent: true });
    }
  }, POLL_INTERVAL_MS);
}

async function loadUsers({ reset = true } = {}) {
  if (!canUseApi()) return;
  const requestId = ++state.usersRequestId;
  if (reset) {
    state.usersOffset = 0;
    state.users = [];
    state.usersHasMore = false;
  }
  setLoadingAndRender("users", true);
  clearError();
  renderApp();
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
  } catch (error) {
    if (requestId === state.usersRequestId) {
      setErrorAndRender(error.message || "Не удалось загрузить пользователей");
    }
  } finally {
    if (requestId === state.usersRequestId) {
      setLoadingAndRender("users", false);
    }
  }
}

async function loadPayouts() {
  if (!canUseApi()) return;
  const requestId = ++state.payoutRequestId;
  setLoadingAndRender("payouts", true);
  clearError();
  renderApp();
  try {
    const payouts = await api("/admin/payouts");
    if (requestId !== state.payoutRequestId) return;
    state.payouts = payouts;
  } catch (error) {
    if (requestId === state.payoutRequestId) {
      setErrorAndRender(error.message || "Не удалось загрузить выплаты");
    }
  } finally {
    if (requestId === state.payoutRequestId) {
      setLoadingAndRender("payouts", false);
    }
  }
}

async function refreshSelectedPayout({ silent = false } = {}) {
  if (!state.selectedPayoutId || !canUseApi()) return;
  const requestId = ++state.payoutRequestId;
  if (!silent) setLoadingAndRender("recipients", true);
  try {
    const detail = await api(`/admin/payouts/${state.selectedPayoutId}`);
    const recipients = await api(`/admin/payouts/${state.selectedPayoutId}/recipients`);
    if (requestId !== state.payoutRequestId) return;
    state.selectedPayoutDetail = detail;
    state.recipients = recipients;
    renderApp();
    startPollingSelectedPayout();
  } catch (error) {
    if (!silent) setErrorAndRender(error.message || "Не удалось обновить выплату");
  } finally {
    if (!silent) setLoadingAndRender("recipients", false);
  }
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
  stopPolling();
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
  startPollingSelectedPayout,
  stopPolling,
};
