import {
  state,
  canUseApi,
  clearError,
  setError,
  setLoading,
  setToast,
  payoutPeriodLabel,
  $,
} from "./store.js";
import { api, scheduleAdminStateRefresh, upsertPayoutInList } from "./api.js";
import { renderApp } from "./render-app.js";
import { parsePeriodInput } from "./utils.js";
let sendInFlight = false;
export function payoutDatesValid() {
  return Boolean(
    parsePeriodInput(state.composer.periodStart) &&
      parsePeriodInput(state.composer.periodEnd),
  );
}
function validate() {
  const a = parsePeriodInput(state.composer.periodStart),
    b = parsePeriodInput(state.composer.periodEnd);
  $("period-start-error").textContent = a
    ? ""
    : "Введите реальную дату в формате ДД.ММ.";
  $("period-end-error").textContent = b
    ? ""
    : "Введите реальную дату в формате ДД.ММ.";
  if (!a || !b) {
    $("payout-validation").textContent = "Проверьте даты периода.";
    $("payout-validation").classList.remove("hidden");
    return null;
  }
  $("payout-validation").classList.add("hidden");
  return {
    period_start_day: a.day,
    period_start_month: a.month,
    period_end_day: b.day,
    period_end_month: b.month,
    message_before_period: state.composer.messageBefore,
    message_after_period: state.composer.messageAfter,
  };
}
export async function createPayout() {
  if (state.loading.createPayout || !canUseApi()) return;
  const payload = validate();
  if (!payload) return;
  const startingPayoutId = Number(state.selectedPayoutId || 0);
  const startingView = state.activeMobileView;
  const isStartingContextCurrent = () =>
    Number(state.selectedPayoutId || 0) === startingPayoutId &&
    state.activeMobileView === startingView;
  setLoading("createPayout", true);
  renderApp();
  clearError();
  try {
    const payout = await api("/admin/payouts", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.selectedUsersByPayout.set(Number(payout.id), new Set());
    if (isStartingContextCurrent()) {
      state.selectedUsers.clear();
      state.selectedPayoutId = payout.id;
      state.selectedPayoutDetail = { payout };
      state.recipients = [];
    }
    upsertPayoutInList(payout);
    setToast(`Выплата #${payout.id} создана.`, "success");
    const c = $("create-success");
    c.innerHTML = "";
    const text = document.createTextNode(
      `Выплата #${payout.id} создана · ${payoutPeriodLabel(payout)}. `,
    );
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button button-quiet";
    button.dataset.action = "go-selection";
    button.dataset.payoutId = String(Number(payout.id));
    button.textContent = "Перейти к выбору пользователей";
    c.append(text, button);
    c.classList.remove("hidden");
    void scheduleAdminStateRefresh({
      selected: isStartingContextCurrent(),
      payouts: true,
      silent: true,
    });
  } catch (e) {
    setError(`Не удалось создать выплату: ${e.message}`);
  } finally {
    setLoading("createPayout", false);
    renderApp();
  }
}
export async function sendPayout() {
  if (
    sendInFlight ||
    state.loading.sendPayout ||
    !canUseApi() ||
    !state.selectedPayoutId ||
    state.selectedPayoutDetail?.payout?.status !== "draft" ||
    !state.selectedUsers.size
  )
    return;
  const id = Number(state.selectedPayoutId);
  const ids = [...state.selectedUsers];
  const submittedUsers = new Set(ids);
  const isCurrentPayout = () => Number(state.selectedPayoutId) === id;
  if (!ids.length || !isCurrentPayout()) return;
  sendInFlight = true;
  setLoading("sendPayout", true);
  renderApp();
  try {
    if (
      !isCurrentPayout() ||
      state.selectedPayoutDetail?.payout?.status !== "draft" ||
      state.loading.sendPayout !== true
    )
      return;
    const snapshot = await api(`/admin/payouts/${id}/send-selected`, {
      method: "POST",
      body: JSON.stringify({ user_ids: ids }),
    });
    const payout = snapshot?.detail?.payout || snapshot?.payout;
    if (payout) upsertPayoutInList(payout);
    const savedSelection = new Set(
      state.selectedUsersByPayout.get(id) || submittedUsers,
    );
    submittedUsers.forEach((userId) => savedSelection.delete(userId));
    state.selectedUsersByPayout.set(id, savedSelection);
    if (isCurrentPayout()) {
      state.selectedPayoutDetail = payout
        ? { payout }
        : state.selectedPayoutDetail;
      state.recipients = snapshot?.recipients || [];
      submittedUsers.forEach((userId) => state.selectedUsers.delete(userId));
      state.selectedUsersByPayout.set(id, new Set(state.selectedUsers));
    }
    setToast(`Рассылка для выплаты #${id} запущена.`, "success");
    void scheduleAdminStateRefresh({
      selected: isCurrentPayout(),
      payouts: true,
      silent: true,
    });
  } catch (e) {
    setError(`Не удалось запустить рассылку для выплаты #${id}: ${e.message}`);
  } finally {
    sendInFlight = false;
    setLoading("sendPayout", false);
    renderApp();
  }
}
