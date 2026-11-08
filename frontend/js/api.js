import { API_TIMEOUT_MS } from "./constants.js";
import {
  state,
  canUseApi,
  clearError,
  setError,
  setLoading,
  setToast,
} from "./store.js";
import { renderApp } from "./render-app.js";
import {
  renderActionState,
  renderCurrentPayout,
  renderNotifications,
  renderPayouts,
} from "./render-common.js";
import { renderUsers } from "./render-users.js";
import { renderRecipients } from "./render-recipients.js";
import {
  createPayout as renderCreatePayout,
  sendPayout as renderSendPayout,
} from "./render-payouts.js";

const TEMPORARY_BACKEND_ERROR =
  "Backend временно недоступен. Проверьте сервер приложения.";
const AUTH_REQUIRED_ERROR = "Требуется вход через Telegram.";
const FORBIDDEN_ERROR = "Недостаточно прав администратора.";

let adminEventsSource = null;
let adminEventsStartPromise = null;
let adminEventsRecoveryUsed = false;
let adminEventsWarningShown = false;
const selectedRefreshPromises = new Map();
let payoutsRefreshPromise = null;
let markPaidInFlight = false;

function isJsonResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json");
}

function isHtmlText(text, contentType = "") {
  const trimmed = text.trimStart();
  return (
    contentType.includes("text/html") ||
    trimmed.startsWith("<!doctype html") ||
    trimmed.startsWith("<html")
  );
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
  if (
    response.status === 502 ||
    response.status === 503 ||
    response.status === 504
  ) {
    return TEMPORARY_BACKEND_ERROR;
  }
  if (response.status === 401) return AUTH_REQUIRED_ERROR;
  if (response.status === 403) return FORBIDDEN_ERROR;
  if (response.status === 409) {
    if (isJsonResponse(response)) {
      try {
        const detail = (await response.json())?.detail;
        if (detail === "payout_closed") return "Выплата закрыта. Отметка оплаты недоступна.";
        if (detail === "payout_cancelled") return "Выплата отменена. Отметка оплаты недоступна.";
        if (detail === "payout_sending") return "Нельзя удалить выплату до завершения рассылки.";
      } catch {
        // Fall back to the snapshot conflict message.
      }
    }
    return "Реквизиты изменились. Проверьте актуальные данные перед подтверждением оплаты.";
  }
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
      return response.status >= 500
        ? TEMPORARY_BACKEND_ERROR
        : "Сервер вернул некорректный ответ.";
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
  const timeoutId = window.setTimeout(
    () => controller.abort("timeout"),
    API_TIMEOUT_MS,
  );
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
      const detail = response.status === 409 && isJsonResponse(response)
        ? (await response.clone().json().catch(() => null))?.detail
        : null;
      const error = new Error(await readErrorMessage(response));
      error.status = response.status;
      error.conflictType = typeof detail === "string" ? detail : null;
      throw error;
    }
    if (response.status === 204) return null;
    if (isJsonResponse(response)) return response.json();
    return response.text();
  } catch (error) {
    if (error?.name === "AbortError" || error === "timeout") {
      throw new Error("Запрос превысил время ожидания.");
    }
    const wrapped = new Error(error?.message || "Request failed.");
    wrapped.status = error?.status;
    wrapped.conflictType = error?.conflictType;
    throw wrapped;
  } finally {
    clearTimeout(timeoutId);
  }
}

function setLoadingAndRender(key, value) {
  setLoading(key, value);
  if (key === "users") renderUsers();
  else if (key === "payouts") renderPayouts();
  else if (key === "recipients") renderSelectedPayoutView();
  else renderApp();
}

function renderSelectedPayoutView() {
  renderCurrentPayout();
  renderRecipients();
  renderPayouts();
  renderActionState();
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
    state.usersLoadError = null;
    state.usersOffset = 0;
    state.users = [];
    state.usersHasMore = false;
  }
  if (!silent) {
    setLoadingAndRender("users", true);
    clearError();
  }
  try {
    const params = new URLSearchParams();
    params.set("limit", String(state.usersLimit));
    params.set("offset", String(state.usersOffset));
    if (state.usersSearch.trim())
      params.set("search", state.usersSearch.trim());
    const page = await api(`/admin/users?${params.toString()}`);
    if (requestId !== state.usersRequestId) return;
    state.users = reset ? page.items : [...state.users, ...page.items];
    state.usersLoadError = null;
    state.usersHasMore = page.has_more;
    state.usersOffset = page.offset + page.items.length;
    renderUsers();
  } catch (error) {
    if (requestId === state.usersRequestId) {
      logLoadError("loadUsers", error);
      state.usersLoadError = error?.message || "Ошибка загрузки";
      renderUsers();
      if (!silent) {
        setError(error.message || "Не удалось загрузить пользователей");
        renderNotifications();
      }
    }
  } finally {
    if (requestId === state.usersRequestId) {
      if (!silent) setLoadingAndRender("users", false);
    }
  }
}

async function loadPayouts({ silent = false, loadMore = false } = {}) {
  if (!canUseApi()) return;
  const requestId = ++state.payoutsRequestId;
  const limit = state.payoutsPageSize;
  const beforeId = loadMore ? Number(state.payouts.at(-1)?.id) : null;
  if (!silent) {
    setLoadingAndRender("payouts", true);
    clearError();
  }
  try {
    const cursor = Number.isInteger(beforeId) && beforeId > 0 ? `&before_id=${beforeId}` : "";
    const payouts = await api(`/admin/payouts?limit=${limit}${cursor}`);
    if (requestId !== state.payoutsRequestId) return;
    state.payoutsLoadError = null;
    if (loadMore) {
      const byId = new Map(state.payouts.map((item) => [Number(item.id), item]));
      payouts.forEach((item) => byId.set(Number(item.id), item));
      state.payouts = [...byId.values()];
      state.payoutsHasMore = payouts.length === limit;
    } else {
      const ids = new Set(payouts.map((item) => Number(item.id)));
      const keepOlder = payouts.length === limit && state.payouts.length > limit;
      state.payouts = [...payouts, ...(keepOlder
        ? state.payouts.filter((item) => !ids.has(Number(item.id)) &&
          Number(item.id) < Number(payouts.at(-1)?.id)) : [])];
      if (!keepOlder) state.payoutsHasMore = payouts.length === limit;
    }
    renderPayouts();
  } catch (error) {
    if (requestId === state.payoutsRequestId) {
      logLoadError("loadPayouts", error);
      state.payoutsLoadError = error?.message || "Ошибка загрузки";
      renderPayouts();
      if (!silent) setErrorAndRender(error.message || "Не удалось загрузить выплаты");
    }
  } finally {
    if (requestId === state.payoutsRequestId && !silent) setLoadingAndRender("payouts", false);
  }
}

export function upsertPayoutInList(payout) {
  if (!payout || payout.id == null) return;
  const payoutId = Number(payout.id);
  const index = state.payouts.findIndex((item) => Number(item.id) === payoutId);
  if (index >= 0) {
    state.payouts[index] = payout;
  } else {
    state.payouts.unshift(payout);
  }
}

async function refreshSelectedPayout({ silent = false } = {}) {
  const payoutId = Number(state.selectedPayoutId);
  const hasInitData = canUseApi();
  if (!payoutId) {
    return false;
  }
  if (!hasInitData) {
    return false;
  }
  const requestId = ++state.selectedPayoutRequestId;
  const isCurrentRequest = () =>
    requestId === state.selectedPayoutRequestId &&
    Number(state.selectedPayoutId) === payoutId;
  if (!silent) setLoadingAndRender("recipients", true);
  else if (state.loading.recipients)
    setLoadingAndRender("recipients", false);
  try {
    const detail = await api(`/admin/payouts/${payoutId}`);
    if (!isCurrentRequest()) return false;
    const recipients = Array.isArray(detail?.recipients)
      ? detail.recipients
      : await api(`/admin/payouts/${payoutId}/recipients`);
    if (!isCurrentRequest()) return false;
    state.selectedPayoutDetail = detail;
    state.recipientsLoadError = null;
    state.recipients = recipients;
    for (const recipient of recipients) {
      if (state.recipientRefreshRequired[Number(recipient.id)])
        delete state.recipientRefreshRequired[Number(recipient.id)];
    }
    upsertPayoutInList(detail?.payout);
    renderSelectedPayoutView();
    return true;
  } catch (error) {
    if (!isCurrentRequest()) return false;
    logLoadError("refreshSelectedPayout", error);
    state.recipientsLoadError = error?.message || "Ошибка загрузки";
    renderSelectedPayoutView();
    if (!silent)
      setErrorAndRender(error.message || "Не удалось обновить выплату");
    return false;
  } finally {
    if (isCurrentRequest() && !silent)
      setLoadingAndRender("recipients", false);
  }
}

export async function fetchSelectedPayoutSnapshot(payoutId) {
  const id = Number(payoutId);
  if (!Number.isInteger(id)) return null;
  const detail = await api(`/admin/payouts/${id}`);
  const recipients = Array.isArray(detail?.recipients)
    ? detail.recipients
    : await api(`/admin/payouts/${id}/recipients`);
  if (Number(state.selectedPayoutId) === id) {
    state.selectedPayoutDetail = detail;
    state.recipients = recipients;
    upsertPayoutInList(detail?.payout);
    renderSelectedPayoutView();
  }
  return { detail, recipients };
}

export async function deleteSelectedPayout(payoutId) {
  const id = Number(payoutId);
  if (!canUseApi() || !Number.isInteger(id) || id < 1) return false;
  await api(`/admin/payouts/${id}`, { method: "DELETE" });
  state.payouts = state.payouts.filter((item) => Number(item.id) !== id);
  if (Number(state.selectedPayoutId) === id) clearPayoutSelection();
  setToast(`Выплата #${id} удалена из истории.`, "success");
  renderApp();
  void schedulePayoutsRefresh({ silent: true });
  return true;
}

export function clearPayoutSelection() {
  const payoutId = Number(state.selectedPayoutId);
  const payoutIds = new Set([...state.selectedUsersByPayout.keys(), ...(payoutId ? [payoutId] : [])]);
  for (const id of payoutIds)
    state.selectionGenerationByPayout.set(id, (state.selectionGenerationByPayout.get(id) || 0) + 1);
  state.selectedPayoutRequestId += 1;
  state.selectedPayoutId = null;
  state.selectedPayoutDetail = null;
  state.recipients = [];
  state.recipientsLoadError = null;
  state.selectedUsers.clear();
  state.selectedUsersByPayout.clear();
  state.expandedRecipients.clear();
  state.recipientOperationError = {};
  state.loading.recipients = false;
  renderApp();
}

export function scheduleSelectedPayoutRefresh({ silent = true } = {}) {
  if (!state.initData || !state.selectedPayoutId) return Promise.resolve();
  const payoutId = Number(state.selectedPayoutId);
  const existing = selectedRefreshPromises.get(payoutId);
  if (
    existing &&
    existing.requestId === state.selectedPayoutRequestId
  ) {
    existing.repeatRequested = true;
    return existing.promise;
  }

  const entry = {
    requestId: null,
    repeatRequested: false,
    promise: null,
  };
  const request = refreshSelectedPayout({ silent });
  entry.requestId = state.selectedPayoutRequestId;
  entry.promise = request
    .then(() => {
      if (
        entry.repeatRequested &&
        entry.requestId === state.selectedPayoutRequestId &&
        Number(state.selectedPayoutId) === payoutId
      ) {
        if (selectedRefreshPromises.get(payoutId) === entry)
          selectedRefreshPromises.delete(payoutId);
        return scheduleSelectedPayoutRefresh({ silent });
      }
      return undefined;
    })
    .finally(() => {
      if (selectedRefreshPromises.get(payoutId) === entry)
        selectedRefreshPromises.delete(payoutId);
    });
  selectedRefreshPromises.set(payoutId, entry);
  return entry.promise;
}

export function schedulePayoutsRefresh({ silent = true } = {}) {
  if (!state.initData) return Promise.resolve();
  if (!payoutsRefreshPromise) {
    payoutsRefreshPromise = loadPayouts({ silent }).finally(() => {
      payoutsRefreshPromise = null;
    });
  }
  return payoutsRefreshPromise;
}

export function scheduleAdminStateRefresh({
  selected = true,
  payouts = true,
  silent = true,
} = {}) {
  const tasks = [];
  if (selected) tasks.push(scheduleSelectedPayoutRefresh({ silent }));
  if (payouts) tasks.push(schedulePayoutsRefresh({ silent }));
  return Promise.allSettled(tasks);
}

export async function createEventsToken() {
  return api("/admin/events-token", { method: "POST" });
}

export function stopAdminEvents({ resetRecovery = true } = {}) {
  if (adminEventsSource) {
    adminEventsSource.close();
    adminEventsSource = null;
  }
  if (resetRecovery) adminEventsRecoveryUsed = false;
  adminEventsStartPromise = null;
}

export function handleAdminEvent(type, payload = {}) {
  if (type === "ping" || !state.initData) return;
  if (type === "users_changed") {
    void loadUsers({ reset: true, silent: true });
    return;
  }
  if (payload.reason === "delete_payout" && payload.payout_id) {
    const id = Number(payload.payout_id);
    state.payouts = state.payouts.filter((item) => Number(item.id) !== id);
    if (Number(state.selectedPayoutId) === id) clearPayoutSelection();
  }
  if (type === "payouts_changed") {
    void schedulePayoutsRefresh({ silent: true });
    return;
  }
  if (type === "payout_changed") {
    if (payload.payout_id && Number(payload.payout_id) === Number(state.selectedPayoutId)) {
      void scheduleAdminStateRefresh({ selected: true, payouts: true, silent: true });
    } else {
      void schedulePayoutsRefresh({ silent: true });
    }
    return;
  }
  if (type === "payout_recipients_changed") {
    if (payload.payout_id && Number(payload.payout_id) === Number(state.selectedPayoutId)) {
      void scheduleAdminStateRefresh({ selected: true, payouts: true, silent: true });
    } else {
      void schedulePayoutsRefresh({ silent: true });
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
  if (!state.initData || !canUseApi()) return null;
  if (adminEventsSource && adminEventsSource.readyState !== EventSource.CLOSED)
    return adminEventsSource;
  if (adminEventsStartPromise) return adminEventsStartPromise;
  adminEventsStartPromise = (async () => {
    try {
      const { token } = await createEventsToken();
      if (
        adminEventsSource &&
        adminEventsSource.readyState !== EventSource.CLOSED
      ) {
        return adminEventsSource;
      }
      const source = new EventSource(
        `/api/admin/events?token=${encodeURIComponent(token)}`,
      );
      adminEventsSource = source;
      adminEventsWarningShown = false;
      source.addEventListener("open", () => {
        adminEventsRecoveryUsed = false;
      });
      source.addEventListener("ping", () => handleAdminEvent("ping", {}));
      source.addEventListener("users_changed", (event) =>
        handleAdminEvent("users_changed", parseEventPayload(event.data)),
      );
      source.addEventListener("payouts_changed", (event) =>
        handleAdminEvent("payouts_changed", parseEventPayload(event.data)),
      );
      source.addEventListener("payout_changed", (event) =>
        handleAdminEvent("payout_changed", parseEventPayload(event.data)),
      );
      source.addEventListener("payout_recipients_changed", (event) =>
        handleAdminEvent(
          "payout_recipients_changed",
          parseEventPayload(event.data),
        ),
      );
      source.onerror = () => {
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

async function selectPayout(id, { requireSuccessfulLoad = false } = {}) {
  if (!canUseApi()) return false;
  const nextId = Number(id);
  const previous = requireSuccessfulLoad
    ? {
        payoutId: state.selectedPayoutId,
        selectedUsers: new Set(state.selectedUsers),
        selectedPayoutDetail: state.selectedPayoutDetail,
        recipients: state.recipients,
        recipientsLoadError: state.recipientsLoadError,
        expandedRecipients: new Set(state.expandedRecipients),
        previousPayoutSelection: state.selectedPayoutId
          ? new Set(
              state.selectedUsersByPayout.get(Number(state.selectedPayoutId)) ||
                state.selectedUsers,
            )
          : null,
        nextPayoutSelection: new Set(
          state.selectedUsersByPayout.get(nextId) || [],
        ),
      }
    : null;
  const changed = Number(state.selectedPayoutId) !== nextId;
  if (changed) {
    if (state.selectedPayoutId)
      state.selectedUsersByPayout.set(
        Number(state.selectedPayoutId),
        new Set(state.selectedUsers),
      );
    state.selectedUsers.clear();
    state.selectedUsersByPayout
      .get(nextId)
      ?.forEach((userId) => state.selectedUsers.add(userId));
    state.selectedPayoutDetail = null;
    state.recipients = [];
    state.expandedRecipients.clear();
  }
  state.selectedPayoutId = nextId;
  const refresh = refreshSelectedPayout();
  const requestId = state.selectedPayoutRequestId;
  const loaded = await refresh;
  const loadedForTarget =
    loaded &&
    Number(state.selectedPayoutDetail?.payout?.id) === nextId;
  if (
    requireSuccessfulLoad &&
    !loadedForTarget &&
    Number(state.selectedPayoutId) === nextId &&
    state.selectedPayoutRequestId === requestId
  ) {
    state.selectedPayoutId = previous.payoutId;
    state.selectedUsers.clear();
    previous.selectedUsers.forEach((userId) => state.selectedUsers.add(userId));
    state.selectedPayoutDetail = previous.selectedPayoutDetail;
    state.recipients = previous.recipients;
    state.recipientsLoadError = previous.recipientsLoadError;
    state.expandedRecipients.clear();
    previous.expandedRecipients.forEach((recipientId) =>
      state.expandedRecipients.add(recipientId),
    );
    if (previous.payoutId)
      state.selectedUsersByPayout.set(
        Number(previous.payoutId),
        previous.previousPayoutSelection,
      );
    state.selectedUsersByPayout.set(nextId, previous.nextPayoutSelection);
    renderApp();
    return false;
  }
  renderApp();
  return loadedForTarget;
}

async function markPaid(recipientId, expectedSnapshotVersion, expectedPayoutId) {
  if (!canUseApi() || !state.selectedPayoutId || markPaidInFlight) return;
  const currentPayout = state.selectedPayoutDetail?.payout;
  if (
    Number(state.selectedPayoutId) !== Number(expectedPayoutId) ||
    Number(currentPayout?.id) !== Number(expectedPayoutId)
  ) return;
  if (["completed", "cancelled"].includes(currentPayout.status)) {
    setToast(
      currentPayout.status === "completed"
        ? "Выплата закрыта. Отметка оплаты недоступна."
        : "Выплата отменена. Отметка оплаты недоступна.",
      "warning",
    );
    renderApp();
    return;
  }
  if (
    ["draft", "sending"].includes(currentPayout.status) ||
    state.recipients.find((item) => Number(item.id) === Number(recipientId))?.status !== "payment_received"
  ) return;
  const payoutId = Number(state.selectedPayoutId);
  const payoutRequestId = state.selectedPayoutRequestId;
  const id = Number(recipientId);
  const isCurrentPayout = () => Number(state.selectedPayoutId) === payoutId && state.selectedPayoutRequestId === payoutRequestId;
  const isSelectedPayout = () => Number(state.selectedPayoutId) === payoutId;
  markPaidInFlight = true;
  setLoadingAndRender("markPaid", true);
  state.loadingRecipientId = id;
  renderApp();
  try {
    await api(
      `/admin/payouts/${payoutId}/recipients/${id}/mark-paid`,
      {
        method: "POST",
        body: JSON.stringify({ expected_snapshot_version: expectedSnapshotVersion }),
      },
    );
    if (isCurrentPayout()) {
      delete state.recipientOperationError[id];
      setToastAndRender(`Оплата получателя в выплате #${payoutId} отмечена.`);
      await refreshSelectedPayout();
    } else {
      setToast(
        `Оплата получателя в выплате #${payoutId} отмечена.`,
        "success",
      );
      renderApp();
    }
    await schedulePayoutsRefresh({ silent: true });
  } catch (error) {
    const message = error?.message || "Ошибка сервера";
    if (error?.status === 409) {
      const payoutClosed = ["payout_closed", "payout_cancelled"].includes(error?.conflictType);
      if (isSelectedPayout() && payoutClosed) {
        state.recipientRefreshRequired[id] = true;
        state.recipientOperationError[id] = message;
        renderApp();
        const refreshed = await refreshSelectedPayout();
        if (!refreshed && isSelectedPayout()) {
          state.recipientOperationError[id] = `${message} Не удалось обновить выплату. Повторите загрузку данных.`;
          renderApp();
        }
      } else if (payoutClosed) {
        setToast(`Выплата #${payoutId}: ${message}`, "warning");
        renderApp();
      } else if (isSelectedPayout()) {
        state.recipientRefreshRequired[id] = true;
        state.expandedRecipients.add(id);
        state.recipientOperationError[id] = "\u0420\u0435\u043a\u0432\u0438\u0437\u0438\u0442\u044b \u0438\u0437\u043c\u0435\u043d\u0438\u043b\u0438\u0441\u044c. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0435 \u0434\u0430\u043d\u043d\u044b\u0435 \u043f\u0435\u0440\u0435\u0434 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435\u043c \u043e\u043f\u043b\u0430\u0442\u044b.";
        renderApp();
        const refreshed = await refreshSelectedPayout();
        if (!refreshed && isSelectedPayout()) {
          state.recipientRefreshRequired[id] = true;
          state.recipientOperationError[id] = "\u0420\u0435\u043a\u0432\u0438\u0437\u0438\u0442\u044b \u0438\u0437\u043c\u0435\u043d\u0438\u043b\u0438\u0441\u044c, \u043d\u043e \u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0435 \u0434\u0430\u043d\u043d\u044b\u0435 \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c. \u041e\u0431\u043d\u043e\u0432\u0438\u0442\u0435 \u0432\u044b\u043f\u043b\u0430\u0442\u0443 \u043f\u0435\u0440\u0435\u0434 \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u044b\u043c \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435\u043c.";
          renderApp();
        }
      } else {
        setToast(`\u0420\u0435\u043a\u0432\u0438\u0437\u0438\u0442\u044b \u0432 \u0432\u044b\u043f\u043b\u0430\u0442\u0435 #${payoutId} \u0438\u0437\u043c\u0435\u043d\u0438\u043b\u0438\u0441\u044c. \u041e\u0442\u043a\u0440\u043e\u0439\u0442\u0435 \u0432\u044b\u043f\u043b\u0430\u0442\u0443 \u0437\u0430\u043d\u043e\u0432\u043e \u0438 \u043f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0434\u0430\u043d\u043d\u044b\u0435.`, "warning");
        renderApp();
      }
    } else if (isCurrentPayout()) {
      state.recipientOperationError[id] = message;
      setErrorAndRender(message || "Не удалось обновить статус");
    } else {
      setToast(
        `Не удалось отметить оплату в выплате #${payoutId}: ${message}`,
        "error",
      );
      renderApp();
    }
  } finally {
    markPaidInFlight = false;
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
