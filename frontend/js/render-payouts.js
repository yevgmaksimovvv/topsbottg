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

const PAYOUT_PERIOD_MONTH_MAX_DAYS = {
  1: 31,
  2: 29,
  3: 31,
  4: 30,
  5: 31,
  6: 30,
  7: 31,
  8: 31,
  9: 30,
  10: 31,
  11: 30,
  12: 31,
};

function parsePositiveInteger(value) {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) ? parsed : null;
}

function validatePeriodPart(day, month, sideLabel) {
  if (!Number.isInteger(day) || day < 1 || day > 31) return `День ${sideLabel} должен быть от 1 до 31.`;
  if (!Number.isInteger(month) || month < 1 || month > 12) return `Месяц ${sideLabel} должен быть от 1 до 12.`;
  if (day > PAYOUT_PERIOD_MONTH_MAX_DAYS[month]) return `Для ${sideLabel} указана невозможная дата.`;
  return "";
}

function validatePayoutForm() {
  const errors = [];
  const periodStartDay = parsePositiveInteger(state.composer.periodStartDay);
  const periodStartMonth = parsePositiveInteger(state.composer.periodStartMonth);
  const periodEndDay = parsePositiveInteger(state.composer.periodEndDay);
  const periodEndMonth = parsePositiveInteger(state.composer.periodEndMonth);

  if (!state.composer.periodStartDay) errors.push("Укажите день начала периода.");
  if (!state.composer.periodStartMonth) errors.push("Укажите месяц начала периода.");
  if (!state.composer.periodEndDay) errors.push("Укажите день окончания периода.");
  if (!state.composer.periodEndMonth) errors.push("Укажите месяц окончания периода.");

  if (periodStartDay !== null && periodStartMonth !== null) {
    const error = validatePeriodPart(periodStartDay, periodStartMonth, "начала периода");
    if (error) errors.push(error);
  }
  if (periodEndDay !== null && periodEndMonth !== null) {
    const error = validatePeriodPart(periodEndDay, periodEndMonth, "окончания периода");
    if (error) errors.push(error);
  }

  document.querySelectorAll("#desktop-payout-validation, #mobile-payout-validation").forEach((node) => {
    node.textContent = errors.join(" ");
    node.classList.toggle("hidden", errors.length === 0);
  });

  return errors.length === 0
    ? {
        periodStartDay,
        periodStartMonth,
        periodEndDay,
        periodEndMonth,
        messageTemplate: state.composer.messageTemplate,
      }
    : null;
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
        message_template: payload.messageTemplate.trim() || null,
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
