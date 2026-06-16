import { PAYOUT_STATUS_LABELS } from "./constants.js";
import { api, loadPayouts, refreshSelectedPayout } from "./api.js";
import { payoutPeriodLabel, state, canUseApi, clearError, setError, setLoading, setToast } from "./store.js";
import { renderApp } from "./render-app.js";
import { emptyStateMarkup, escapeHtml, statusLabel } from "./utils.js";
import { payoutsEmptyMessage } from "./render-common.js";

const DRAFT_SEND_MESSAGE = "Выплату можно разослать только из черновика. Обновите список выплат.";

export function renderPayoutsInto(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return;
  const empty = payoutsEmptyMessage();
  if (empty) {
    root.innerHTML = emptyStateMarkup("Выплаты", empty);
    return;
  }
  root.innerHTML = state.payouts
    .map((payout) => {
      const selected = state.selectedPayoutId === payout.id;
      const periodLabel = payoutPeriodLabel(payout) || "Период не задан";
      return `
        <button type="button" class="payout-card ${selected ? "selected" : ""}" data-payout-id="${payout.id}">
          <div class="payout-main">
            <strong>#${payout.id} · ${escapeHtml(periodLabel)}</strong>
            <span class="meta">${escapeHtml(statusLabel(PAYOUT_STATUS_LABELS, payout.status))}</span>
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
  setToast(message);
  renderApp();
}

export async function createPayout() {
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
        period_start_day: payload.periodStartDay,
        period_start_month: payload.periodStartMonth,
        period_end_day: payload.periodEndDay,
        period_end_month: payload.periodEndMonth,
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

export async function attachSelected() {
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

export async function sendPayout() {
  if (!canUseApi() || !state.selectedPayoutId) return;
  await refreshSelectedPayout({ silent: true });
  const payout = state.selectedPayoutDetail?.payout;
  if (!payout || payout.id !== state.selectedPayoutId) {
    setErrorAndRender("Не удалось обновить выплату.");
    return;
  }
  if (payout.status !== "draft") {
    setErrorAndRender(DRAFT_SEND_MESSAGE);
    return;
  }
  const count = state.recipients.length;
  const confirmed = window.confirm(
    `Запустить рассылку выплаты "${payoutPeriodLabel(payout) || state.selectedPayoutId}" для ${count} получателей?`
  );
  if (!confirmed) return;
  setLoadingAndRender("sendPayout", true);
  clearError();
  try {
    await api(`/admin/payouts/${state.selectedPayoutId}/send`, { method: "POST" });
    setToastAndRender("Рассылка запущена.");
    await refreshSelectedPayout();
    await loadPayouts();
  } catch (error) {
    const message =
      error?.message && error.message.includes("payout can only be sent from draft")
        ? DRAFT_SEND_MESSAGE
        : "Не удалось разослать выплату.";
    setErrorAndRender(message);
  } finally {
    setLoadingAndRender("sendPayout", false);
  }
}
