import { state, canUseApi, $, payoutPeriodLabel } from "./store.js";
import { PAYOUT_STATUS_LABELS } from "./constants.js";
import {
  composerMessageText,
  countComposerMessage,
  MAX_PAYOUT_MESSAGE_LENGTH,
} from "./message-limit.js";
import { escapeHtml } from "./utils.js";
const nameFor = (m, k) => m[k] || "Неизвестный статус";
export function renderPayoutsInto(id = "payouts-list") {
  const root = $(id);
  if (!root) return;
  const list = [...state.payouts].sort((a, b) => Number(b.id) - Number(a.id));
  $("payouts-state").textContent = !canUseApi()
    ? "Откройте приложение через Telegram, чтобы загрузить историю."
    : state.loading.payouts
      ? "Загрузка выплат…"
      : state.payoutsLoadError
        ? `Не удалось загрузить историю: ${state.payoutsLoadError}. Повторите попытку.`
        : list.length
          ? ""
          : "Выплат пока нет.";
  root.innerHTML = list
    .map(
      (p) =>
        `<button type="button" class="payout-row" data-payout-id="${Number(p.id)}"><span class="payout-main"><strong>#${Number(p.id)} · ${escapeHtml(payoutPeriodLabel(p) || "Период не задан")}</strong><span>${escapeHtml(nameFor(PAYOUT_STATUS_LABELS, p.status))}</span></span><span class="current-marker">${Number(p.id) === Number(state.selectedPayoutId) ? "Текущая" : "Выбрать"}</span></button>`,
    )
    .join("");
}
export function renderPayouts() {
  renderPayoutsInto();
}
export function renderCurrentPayout() {
  const p = state.selectedPayoutDetail?.payout;
  const context = p
    ? `Текущая: #${state.selectedPayoutId} · ${payoutPeriodLabel(p)} · ${nameFor(PAYOUT_STATUS_LABELS, p.status)}`
    : "Текущая выплата не выбрана.";
  for (const id of [
    "history-current-context",
    "selection-payout-context",
    "recipients-context",
  ]) {
    const el = $(id);
    if (el) el.textContent = context;
  }
}
export function renderActionState() {
  const b = document.querySelector('[data-action="create-payout"]');
  if (b) {
    b.disabled = !canUseApi() || state.loading.createPayout;
  }
  const send = document.querySelector('[data-action="send-payout"]');
  if (send)
    send.disabled = state.loading.sendPayout || !state.selectedUsers.size;
  $("selection-bar")?.classList.toggle(
    "hidden",
    state.activeMobileView !== "users" || !state.selectedUsers.size,
  );
}
export function renderPreview() {
  const n = $("message-count");
  if (n)
    n.textContent = `${countComposerMessage(state.composer)} / ${MAX_PAYOUT_MESSAGE_LENGTH}`;
  const p = $("preview");
  if (p) {
    p.classList.toggle("hidden", !state.previewOpen);
    if (state.previewOpen) p.textContent = composerMessageText(state.composer);
  }
  const toggle = document.querySelector('[data-action="toggle-preview"]');
  if (toggle) {
    toggle.textContent = state.previewOpen
      ? "Скрыть сообщение"
      : "Посмотреть сообщение";
    toggle.setAttribute("aria-expanded", String(state.previewOpen));
  }
}
export function renderNotifications() {
  const el = $("toast");
  if (!el) return;
  const n = state.notification;
  el.textContent = n?.message || "";
  el.className = n ? `toast toast-${n.kind}` : "toast hidden";
  el.setAttribute("role", n?.kind === "error" ? "alert" : "status");
}
