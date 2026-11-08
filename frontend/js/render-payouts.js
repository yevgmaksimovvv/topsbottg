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
  setLoading("createPayout", true);
  renderApp();
  clearError();
  try {
    const payout = await api("/admin/payouts", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.selectedUsers.clear();
    state.selectedPayoutId = payout.id;
    state.selectedPayoutDetail = { payout };
    state.recipients = [];
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
    button.textContent = "Перейти к выбору пользователей";
    c.append(text, button);
    c.classList.remove("hidden");
    void scheduleAdminStateRefresh({
      selected: true,
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
    !state.selectedUsers.size
  )
    return;
  sendInFlight = true;
  setLoading("sendPayout", true);
  renderApp();
  try {
    const id = Number(state.selectedPayoutId),
      ids = [...state.selectedUsers];
    const snapshot = await api(`/admin/payouts/${id}/send-selected`, {
      method: "POST",
      body: JSON.stringify({ user_ids: ids }),
    });
    const payout = snapshot?.detail?.payout || snapshot?.payout;
    if (payout) {
      state.selectedPayoutDetail = { payout };
      upsertPayoutInList(payout);
    }
    state.recipients = snapshot?.recipients || [];
    state.selectedUsers.clear();
    state.selectedUsersByPayout.set(id, new Set());
    setToast("Рассылка запущена.", "success");
    void scheduleAdminStateRefresh({
      selected: true,
      payouts: true,
      silent: true,
    });
  } catch (e) {
    setError(`Не удалось запустить рассылку: ${e.message}`);
  } finally {
    sendInFlight = false;
    setLoading("sendPayout", false);
    renderApp();
  }
}
