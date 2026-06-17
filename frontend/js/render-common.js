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
  if (toast) {
    const notification = state.notification;
    if (!notification) {
      toast.textContent = "";
      toast.className = "toast hidden";
      return;
    }
    const label = notification.kind === "error" ? "Ошибка" : notification.kind === "warning" ? "Предупреждение" : "Сообщение";
    toast.className = `toast toast-${notification.kind}`;
    toast.setAttribute("role", notification.kind === "error" ? "alert" : "status");
    toast.innerHTML = `
      <div class="toast-copy">
        <strong>${escapeHtml(label)}</strong>
        <span>${escapeHtml(notification.message)}</span>
      </div>
      <button type="button" class="toast-close" data-action="dismiss-toast" aria-label="Закрыть уведомление">×</button>`;
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

export function selectedPayoutHintText(payout) {
  switch (payout?.status) {
    case "draft":
      return "Черновик выбран. Добавьте получателей во вкладке «Пользователи».";
    case "sent":
      return "Разослана. Статусы доступны во вкладке «Получатели».";
    case "partially_failed":
      return "Разослана с ошибками. Проверьте вкладку «Получатели».";
    case "completed":
    case "paid":
      return "Выплата завершена. Детали во вкладке «Получатели».";
    case "cancelled":
      return "Выплата отменена.";
    default:
      return "Выплата выбрана.";
  }
}

export function selectedPayoutSummaryText() {
  if (!state.selectedPayoutDetail) return "Выберите выплату в списке.";
  const { payout } = state.selectedPayoutDetail;
  const periodLabel = payoutPeriodLabel(payout) || "Период не задан";
  const recipientsCount = state.recipients.length;
  const recipientsLabel =
    recipientsCount === 1
      ? "1 получатель"
      : recipientsCount % 10 >= 2 && recipientsCount % 10 <= 4 && (recipientsCount % 100 < 10 || recipientsCount % 100 >= 20)
        ? `${recipientsCount} получателя`
        : `${recipientsCount} получателей`;
  return `Выплата #${payout.id}\n${periodLabel} · ${statusLabel(PAYOUT_STATUS_LABELS, payout.status)} · ${recipientsLabel}`;
}

export function canSendSelectedPayout() {
  const payout = state.selectedPayoutDetail?.payout;
  return Boolean(
    canUseApi() &&
      payout &&
      payout.status === "draft" &&
      (state.selectedUsers.size > 0 || state.recipients.length > 0)
  );
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
  const sendAction = payout.status === "draft" ? `<button type="button" class="success-button" data-action="send-payout" ${sendLoading || !canSendSelectedPayout() ? "disabled" : ""}>${sendLoading ? '<span class="spinner"></span> Разосылаем…' : "Разослать"}</button>` : "";
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
      </div>
      ${sendAction ? `<div class="current-payout-actions">${sendAction}</div>` : ""}
    </div>`;
}

export function renderCurrentPayout() {
  renderCurrentPayoutInto("desktop-current-payout");
  renderCurrentPayoutInto("mobile-current-payout");
}

export function renderActionState() {
  setButtonState("send-payout", {
    label: state.loading.sendPayout ? "Разосылаем…" : "Разослать",
    loading: state.loading.sendPayout,
    disabled: state.loading.sendPayout || !canSendSelectedPayout(),
    hint: "",
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
  setButtonState("create-payout", {
    label: state.loading.createPayout ? "Создаём…" : "Создать выплату",
    loading: state.loading.createPayout,
    disabled: state.loading.createPayout || !canUseApi(),
    hint: canUseApi() ? "Создать новую выплату." : "Нужен доступ к данным.",
  });
  document.querySelectorAll('[data-action="dismiss-toast"]').forEach((button) => {
    button.classList.toggle("hidden", !state.notification);
  });
}
