import { POLL_INTERVAL_MS } from "./constants.js";
import { state, canUseApi, clearError, setError, setLoading, setToast } from "./store.js";
import { renderApp } from "./render-app.js";

async function readErrorMessage(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const data = await response.json();
    return data.detail || JSON.stringify(data);
  }
  return response.text();
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
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return response.json();
  return response.text();
}

function validatePayoutForm() {
  const errors = [];
  if (!state.composer.title.trim()) errors.push("Введите название выплаты.");
  if (!state.composer.periodFrom) errors.push("Укажите дату начала периода.");
  if (!state.composer.periodTo) errors.push("Укажите дату окончания периода.");
  if (state.composer.periodFrom && state.composer.periodTo && state.composer.periodFrom > state.composer.periodTo) {
    errors.push("Период начала не может быть позже периода окончания.");
  }

  document.querySelectorAll("#desktop-payout-validation, #mobile-payout-validation").forEach((node) => {
    node.textContent = errors.join(" ");
    node.classList.toggle("hidden", errors.length === 0);
  });

  return errors.length === 0 ? { ...state.composer } : null;
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
    if (state.usersHasPayment !== "") params.set("has_payment_profile", state.usersHasPayment);
    if (state.usersIsActive !== "") params.set("is_active", state.usersIsActive);
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
  state.selectedPayoutId = id;
  stopPolling();
  await refreshSelectedPayout();
  renderApp();
}

async function createPayout() {
  if (!canUseApi()) return;
  const payload = validatePayoutForm();
  if (!payload) return;
  if (!state.selectedUsers.size) {
    const confirmed = window.confirm("Создать выплату без получателей?");
    if (!confirmed) return;
  }
  setLoadingAndRender("createPayout", true);
  clearError();
  try {
    const payout = await api("/admin/payouts", {
      method: "POST",
      body: JSON.stringify({
        title: payload.title,
        period_from: payload.periodFrom,
        period_to: payload.periodTo,
        message_template: payload.messageTemplate.trim() || null,
      }),
    });
    if (state.selectedUsers.size) {
      await api(`/admin/payouts/${payout.id}/recipients`, {
        method: "POST",
        body: JSON.stringify({ user_ids: [...state.selectedUsers] }),
      });
    }
    setToastAndRender(`Выплата #${payout.id} создана.`);
    await loadPayouts();
    state.selectedPayoutId = payout.id;
    await refreshSelectedPayout();
  } catch (error) {
    setErrorAndRender(error.message || "Не удалось создать выплату");
  } finally {
    setLoadingAndRender("createPayout", false);
  }
}

async function attachSelected() {
  if (!canUseApi() || !state.selectedPayoutId || !state.selectedUsers.size) return;
  setLoadingAndRender("attachSelected", true);
  try {
    await api(`/admin/payouts/${state.selectedPayoutId}/recipients`, {
      method: "POST",
      body: JSON.stringify({ user_ids: [...state.selectedUsers] }),
    });
    setToastAndRender("Выбранные пользователи добавлены к выплате.");
    await refreshSelectedPayout();
    await loadPayouts();
  } catch (error) {
    setErrorAndRender(error.message || "Не удалось добавить пользователей");
  } finally {
    setLoadingAndRender("attachSelected", false);
  }
}

async function sendPayout() {
  if (!canUseApi() || !state.selectedPayoutId) return;
  if (!state.selectedPayoutDetail || state.selectedPayoutDetail.payout.id !== state.selectedPayoutId) {
    await refreshSelectedPayout({ silent: true });
  }
  const payout = state.selectedPayoutDetail?.payout;
  const count = state.recipients.length;
  const confirmed = window.confirm(
    `Запустить рассылку выплаты "${payout?.title || state.selectedPayoutId}" для ${count} получателей?`
  );
  if (!confirmed) return;
  setLoadingAndRender("sendPayout", true);
  try {
    await api(`/admin/payouts/${state.selectedPayoutId}/send`, { method: "POST" });
    setToastAndRender("Рассылка запущена.");
    await refreshSelectedPayout();
    await loadPayouts();
  } catch (error) {
    setErrorAndRender(error.message || "Не удалось запустить рассылку");
  } finally {
    setLoadingAndRender("sendPayout", false);
  }
}

async function exportCsv() {
  if (!canUseApi() || !state.selectedPayoutId) return;
  setLoadingAndRender("exportCsv", true);
  try {
    const response = await fetch(`/api/admin/payouts/${state.selectedPayoutId}/export.csv`, {
      headers: {
        "X-Telegram-Init-Data": state.initData,
      },
    });
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `payout-${state.selectedPayoutId}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setToastAndRender("CSV выгружен.");
  } catch (error) {
    setErrorAndRender(error.message || "Не удалось выгрузить CSV");
  } finally {
    setLoadingAndRender("exportCsv", false);
  }
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

function openPaymentModal(details) {
  state.modalPaymentDetails = details;
  renderApp();
}

function closePaymentModal() {
  state.modalPaymentDetails = null;
  renderApp();
}

async function revealPaymentDetails(userId) {
  if (!canUseApi()) return;
  setLoadingAndRender("revealPaymentDetails", true);
  state.loadingUserId = userId;
  renderApp();
  try {
    const details = await api(`/admin/users/${userId}/payment-details`);
    openPaymentModal(details);
  } catch (error) {
    setErrorAndRender(error.message || "Не удалось открыть платёжные данные");
  } finally {
    state.loadingUserId = null;
    setLoadingAndRender("revealPaymentDetails", false);
    renderApp();
  }
}

async function copyPaymentDetails() {
  if (!state.modalPaymentDetails) return;
  try {
    await navigator.clipboard.writeText(state.modalPaymentDetails.raw_payment_details);
    setToastAndRender("Платёжные данные скопированы.");
  } catch {
    setErrorAndRender("Не удалось скопировать платёжные данные.");
  }
}

export {
  api,
  attachSelected,
  closePaymentModal,
  copyPaymentDetails,
  createPayout,
  exportCsv,
  loadPayouts,
  loadUsers,
  markPaid,
  openPaymentModal,
  refreshSelectedPayout,
  revealPaymentDetails,
  selectPayout,
  sendPayout,
  startPollingSelectedPayout,
  stopPolling,
};
