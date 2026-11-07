import { PAYOUT_STATUS_LABELS } from "./constants.js";
import {
  api,
  scheduleAdminStateRefresh,
  upsertPayoutInList,
} from "./api.js";
import { payoutPeriodLabel, state, canUseApi, clearError, setError, setLoading, setToast } from "./store.js";
import { renderApp } from "./render-app.js";
import { emptyStateMarkup, escapeHtml, loadingStateMarkup, statusLabel } from "./utils.js";
import { payoutsEmptyMessage, selectedPayoutHintText } from "./render-common.js";

const DRAFT_SEND_MESSAGE = "Выплату можно разослать только из черновика. Обновите список выплат.";
let sendPayoutInFlight = false;

export function renderPayoutsInto(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return;
  if (state.loading.payouts && !state.payouts.length) {
    root.innerHTML = loadingStateMarkup("Выплаты", "Загрузка…");
    return;
  }
  const empty = payoutsEmptyMessage();
  if (empty) {
    root.innerHTML = emptyStateMarkup("Выплаты", empty);
    return;
  }
  root.innerHTML = state.payouts
    .slice()
    .sort((a, b) => Number(b.id) - Number(a.id))
    .map((payout) => {
      const selected = state.selectedPayoutId === payout.id;
      const periodLabel = payoutPeriodLabel(payout) || "Период не задан";
      return `
        <button type="button" class="payout-card ${selected ? "selected" : ""}" data-payout-id="${payout.id}">
          <div class="payout-main">
            <strong>#${payout.id} · ${escapeHtml(periodLabel)}</strong>
            <span class="meta">${escapeHtml(statusLabel(PAYOUT_STATUS_LABELS, payout.status))}</span>
            ${selected ? `<span class="meta">${escapeHtml(selectedPayoutHintText(payout))}</span>` : ""}
          </div>
          <span class="badge ${selected ? "badge-success" : "badge-muted"}">${selected ? "Выбрана" : "Открыть"}</span>
        </button>`;
    })
    .join("");
}

export function renderPayouts() {
  renderPayoutsInto("desktop-payouts");
  renderPayoutsInto("mobile-payouts");
}

const PAYOUT_PERIOD_MONTH_MAX_DAYS = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
function parsePeriod(value, side) {
  const match = /^(\d{2})\.(\d{2})$/.exec(value);
  if (!match) return { error: `\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0434\u0430\u0442\u0443 ${side} \u043f\u0435\u0440\u0438\u043e\u0434\u0430 \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0435 \u0414\u0414.\u041c\u041c.` };
  const day = Number(match[1]), month = Number(match[2]);
  if (month < 1 || month > 12 || day < 1 || day > PAYOUT_PERIOD_MONTH_MAX_DAYS[month - 1]) return { error: `\u0423\u043a\u0430\u0437\u0430\u043d\u0430 \u043d\u0435\u0432\u043e\u0437\u043c\u043e\u0436\u043d\u0430\u044f \u0434\u0430\u0442\u0430 ${side} \u043f\u0435\u0440\u0438\u043e\u0434\u0430.` };
  return { day, month };
}
function validatePayoutForm() {
  const start = parsePeriod(state.composer.periodStart, "\u043d\u0430\u0447\u0430\u043b\u0430");
  const end = parsePeriod(state.composer.periodEnd, "\u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u044f");
  const errors = [start.error, end.error].filter(Boolean);
  document.querySelectorAll("#desktop-payout-validation, #mobile-payout-validation").forEach((node) => {
    node.textContent = errors.join(" "); node.classList.toggle("hidden", !errors.length);
  });
  return errors.length ? null : {
    periodStartDay: start.day, periodStartMonth: start.month,
    periodEndDay: end.day, periodEndMonth: end.month,
    messageBefore: state.composer.messageBefore, messageAfter: state.composer.messageAfter,
  };
}
export function payoutDatesValid() {
  return Boolean(parsePeriod(state.composer.periodStart, "\u043d\u0430\u0447\u0430\u043b\u0430").day && parsePeriod(state.composer.periodEnd, "\u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u044f").day);
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

export async function createPayout() {
  if (state.loading?.createPayout) return;
  if (!canUseApi()) {
    setErrorAndRender("Админский доступ ещё не готов. Подождите секунду и повторите.");
    return;
  }
  const payload = validatePayoutForm();
  if (!payload) return;
  setLoadingAndRender("createPayout", true);
  clearError();
  try {
    const payout = await api("/admin/payouts", {
      method: "POST",
      body: JSON.stringify({
        period_start_day: payload.periodStartDay,
        period_start_month: payload.periodStartMonth,
        period_end_day: payload.periodEndDay,
        period_end_month: payload.periodEndMonth,
        message_before_period: payload.messageBefore,
        message_after_period: payload.messageAfter,
      }),
    });
    state.selectedUsers.clear();
    state.selectedPayoutId = payout.id;
    state.selectedPayoutDetail = { payout };
    state.recipients = [];
    upsertPayoutInList(payout);
    clearError();
    setToastAndRender(`Выплата #${payout.id} создана.`);
    void scheduleAdminStateRefresh({ selected: true, payouts: true, silent: true });
  } catch (error) {
    setErrorAndRender(error.message || "Не удалось создать выплату");
  } finally {
    setLoadingAndRender("createPayout", false);
  }
}

export async function sendPayout() {
  if (sendPayoutInFlight || state.loading?.sendPayout) return;
  sendPayoutInFlight = true;
  try {
    if (!canUseApi() || !state.selectedPayoutId) return;
    const payoutId = Number(state.selectedPayoutId);
    const selectedUserIds = Array.from(state.selectedUsers);
    const confirmed = window.confirm(
      `Запустить рассылку выплаты "${payoutPeriodLabel(state.selectedPayoutDetail?.payout) || payoutId}"?`
    );
    if (!confirmed) return;
    setLoadingAndRender("sendPayout", true);
    clearError();
    try {
      const snapshot = await api(`/admin/payouts/${payoutId}/send-selected`, {
        method: "POST",
        body: JSON.stringify({ user_ids: selectedUserIds }),
      });
      const payout = snapshot?.detail?.payout || snapshot?.payout || null;
      const recipients = snapshot?.recipients || [];
      if (payout) {
        state.selectedPayoutDetail = { payout };
        upsertPayoutInList(payout);
      }
      state.recipients = recipients;
      state.selectedUsers.clear();
      clearError();
      setToastAndRender("Рассылка запущена.");
      void scheduleAdminStateRefresh({ selected: true, payouts: true, silent: true });
    } catch (error) {
      const message =
        error?.message && error.message.includes("payout can only be sent from draft")
          ? DRAFT_SEND_MESSAGE
          : error?.message || "Не удалось разослать выплату.";
      setErrorAndRender(message);
    } finally {
      setLoadingAndRender("sendPayout", false);
    }
  } finally {
    sendPayoutInFlight = false;
  }
}
