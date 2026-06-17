import { PAYOUT_STATUS_LABELS } from "./constants.js";
import { $, canUseApi, payoutPeriodLabel, formatPayoutPeriodLabel, state } from "./store.js";
import { badgeClassForStatus, emptyStateMarkup, escapeHtml, formatTime, statusLabel } from "./utils.js";

function setTextAll(ids, text) {
  ids.forEach((id) => {
    const node = $(id);
    if (node) node.textContent = text;
  });
}

function composerPeriodLabel() {
  const startDay = state.composer.periodStartDay;
  const startMonth = state.composer.periodStartMonth;
  const endDay = state.composer.periodEndDay;
  const endMonth = state.composer.periodEndMonth;
  if (!startDay || !startMonth || !endDay || !endMonth) return "";
  return formatPayoutPeriodLabel(startDay, startMonth, endDay, endMonth);
}

export function renderNotifications() {
  const toast = $("toast");
  const errorBanner = $("error-banner");
  if (toast) {
    if (state.toast) {
      toast.textContent = state.toast;
      toast.classList.remove("hidden");
    } else {
      toast.textContent = "";
      toast.classList.add("hidden");
    }
  }
  if (errorBanner) {
    if (state.error) {
      errorBanner.textContent = state.error;
      errorBanner.classList.remove("hidden");
    } else {
      errorBanner.textContent = "";
      errorBanner.classList.add("hidden");
    }
  }
}

export function renderSelectedCount() {
  setTextAll(["selected-count", "selected-count-mobile"], String(state.selectedUsers.size));
}

export function renderMobileView() {
  document.querySelectorAll("[data-mobile-view]").forEach((button) => {
    const active = button.dataset.mobileView === state.activeMobileView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  document.querySelectorAll("[data-mobile-panel]").forEach((panel) => {
    const active = panel.dataset.mobilePanel === state.activeMobileView;
    panel.hidden = !active;
    panel.classList.toggle("hidden", !active);
    panel.setAttribute("aria-hidden", String(!active));
    if ("inert" in panel) panel.inert = !active;
  });
}

export function setButtonState(action, { label, loading = false, disabled = false, hint = "" }) {
  document.querySelectorAll(`[data-action="${action}"]`).forEach((button) => {
    button.disabled = disabled;
    if (hint) button.title = hint;
    else button.removeAttribute("title");
    button.innerHTML = loading ? `<span class="spinner"></span> ${escapeHtml(label)}` : escapeHtml(label);
  });
}

export function formatPayoutPreviewText() {
  const payoutDetail = state.selectedPayoutDetail?.payout || null;
  const template = state.composer.messageTemplate.trim() || payoutDetail?.message_template || "";
  if (!template.trim()) return "";
  const periodLabel = composerPeriodLabel() || payoutPeriodLabel(payoutDetail);
  const periodStart = periodLabel ? periodLabel.split(" — ")[0] : "";
  const periodEnd = periodLabel ? periodLabel.split(" — ")[1] : "";
  return template
    .replaceAll("{period_start}", periodStart)
    .replaceAll("{period_end}", periodEnd)
    .replaceAll("{period_label}", periodLabel);
}

export function renderPreviewInto(rootId) {
  const root = $(rootId);
  if (!root) return;
  const text = formatPayoutPreviewText();
  if (!text) {
    root.innerHTML = `
      <div class="preview-empty">
        <strong>Предпросмотр</strong>
        <p>Введите шаблон, чтобы увидеть сообщение.</p>
      </div>`;
    return;
  }
  root.innerHTML = `
    <div class="telegram-card">
      <div class="telegram-card-head">
        <div class="telegram-avatar">TB</div>
        <div>
          <strong>TopsBot</strong>
          <div class="meta">Платёжное сообщение</div>
        </div>
      </div>
      <div class="telegram-bubble">${escapeHtml(text).replaceAll("\n", "<br />")}</div>
      <div class="telegram-time">${formatTime(new Date())}</div>
    </div>`;
}

export function renderPreview() {
  renderPreviewInto("desktop-preview");
  renderPreviewInto("mobile-preview");
}

export function usersEmptyMessage() {
  if (!canUseApi()) return "Пользователи появятся после открытия через Telegram.";
  if (!state.users.length) return "Пользователи не найдены.";
  return "";
}

export function payoutsEmptyMessage() {
  if (!canUseApi()) return "Выплаты появятся после открытия через Telegram.";
  if (!state.payouts.length) return "Выплат пока нет.";
  return "";
}

export function recipientsEmptyMessage() {
  if (!canUseApi()) return "Выберите выплату, чтобы увидеть получателей.";
  if (!state.selectedPayoutId) return "Выберите выплату.";
  if (!state.recipients.length) return "У этой выплаты пока нет получателей.";
  return "";
}

export function selectedPayoutSummaryText() {
  if (!state.selectedPayoutDetail) return "Выплата не выбрана";
  const { payout } = state.selectedPayoutDetail;
  const periodLabel = payoutPeriodLabel(payout) || "Период не задан";
  return `Для выплаты #${payout.id} · ${periodLabel} · ${statusLabel(PAYOUT_STATUS_LABELS, payout.status)} · ${state.recipients.length} получ.`;
}

export function renderCurrentPayoutInto(rootId) {
  const root = $(rootId);
  if (!root) return;
  if (!canUseApi() || !state.selectedPayoutDetail) {
    root.innerHTML = emptyStateMarkup("Текущая выплата", "Выберите выплату.");
    return;
  }
  const payout = state.selectedPayoutDetail.payout;
  const sendLoading = state.loading.sendPayout;
  const periodLabel = payoutPeriodLabel(payout) || "Период не задан";
  const hasRecipients = state.recipients.length > 0;
  const hasSelection = state.selectedUsers.size > 0;
  const sendAction =
    payout.status === "draft"
      ? `<button type="button" data-action="send-payout" ${sendLoading || (!hasRecipients && !hasSelection) ? "disabled" : ""}>${
          sendLoading
            ? '<span class="spinner"></span> Разосылаем…'
            : hasRecipients || hasSelection
              ? "Разослать"
              : "Выберите пользователей"
        }</button>`
      : "";
  root.innerHTML = `
    <div class="current-payout-card">
      <div class="current-payout-head">
        <div>
          <strong>#${payout.id}</strong>
          <div class="meta">Период: ${escapeHtml(periodLabel)}</div>
          <div class="meta">${escapeHtml(statusLabel(PAYOUT_STATUS_LABELS, payout.status))}</div>
        </div>
        <span class="badge ${badgeClassForStatus(payout.status)}">${escapeHtml(statusLabel(PAYOUT_STATUS_LABELS, payout.status))}</span>
      </div>
      <div class="current-payout-stats">
        <span>${state.recipients.length} получателей</span>
        ${payout.status === "draft" ? `<span>${escapeHtml(hasSelection ? `Выбрано ${state.selectedUsers.size} пользователей для этой выплаты` : "Пользователи выбираются во вкладке «Пользователи».")}</span>` : ""}
      </div>
      ${sendAction ? `<div class="current-payout-actions">${sendAction}</div>` : ""}
    </div>`;
}

export function renderCurrentPayout() {
  renderCurrentPayoutInto("desktop-current-payout");
  renderCurrentPayoutInto("mobile-current-payout");
}

export function renderActionState() {
  setButtonState("reload-users", {
    label: state.loading.users ? "Обновляем…" : "Обновить",
    loading: state.loading.users,
    disabled: state.loading.users || !canUseApi(),
    hint: canUseApi() ? "Перезагрузить список пользователей." : "Нужен доступ к данным.",
  });
  document.querySelectorAll('[data-action="reload-users"]').forEach((button) => {
    button.classList.toggle("hidden", !canUseApi());
  });
  setButtonState("load-more-users", {
    label: state.loading.users ? "Загружаем…" : "Загрузить ещё",
    loading: state.loading.users,
    disabled: state.loading.users || !state.usersHasMore || !canUseApi(),
    hint: !canUseApi()
      ? "Нужен доступ к данным."
      : state.usersHasMore
        ? "Подгрузить следующую страницу пользователей."
        : "Больше пользователей нет.",
  });
  document.querySelectorAll('[data-action="load-more-users"]').forEach((button) => {
    button.classList.toggle("hidden", !canUseApi() || !state.usersHasMore);
  });
  setButtonState("clear-selection", {
    label: "Снять выбор",
    disabled: state.selectedUsers.size === 0 || !canUseApi(),
    hint: state.selectedUsers.size ? "Очистить выбранных пользователей." : "Ничего не выбрано.",
  });
  document.querySelectorAll('[data-action="clear-selection"]').forEach((button) => {
    button.classList.toggle("hidden", !canUseApi() || state.selectedUsers.size === 0);
  });
  setButtonState("create-payout", {
    label: state.loading.createPayout ? "Создаём…" : "Создать выплату",
    loading: state.loading.createPayout,
    disabled: state.loading.createPayout || !canUseApi(),
    hint: canUseApi() ? "Создать новую выплату." : "Нужен доступ к данным.",
  });
}
