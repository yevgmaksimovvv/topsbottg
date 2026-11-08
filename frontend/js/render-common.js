import { state, canUseApi, $, payoutPeriodLabel } from "./store.js";
import { PAYOUT_WORKFLOW_LABELS } from "./constants.js";
import {
  composerMessageText,
  countComposerMessage,
  MAX_PAYOUT_MESSAGE_LENGTH,
} from "./message-limit.js";
import { escapeHtml } from "./utils.js";
const nameFor = (m, k) => m[k] || "Неизвестный статус";
const payoutProgress = (p) => `\u0412\u044b\u043f\u043b\u0430\u0447\u0435\u043d\u043e: ${Number(p.paid_recipient_count || 0)} \u0438\u0437 ${Number(p.active_recipient_count ?? p.recipient_count ?? 0)}${Number(p.cancelled_recipient_count || 0) ? ` \u00b7 \u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e: ${Number(p.cancelled_recipient_count)}` : ""}`;
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
        `<button type="button" class="payout-row" data-payout-id="${Number(p.id)}"><span class="payout-main"><strong>#${Number(p.id)} · ${escapeHtml(payoutPeriodLabel(p) || "Период не задан")}</strong><span>${escapeHtml(nameFor(PAYOUT_WORKFLOW_LABELS, p.workflow_state))}</span><small>${payoutProgress(p)}</small></span><span class="current-marker">${Number(p.id) === Number(state.selectedPayoutId) ? "Текущая" : "Выбрать"}</span></button>`,
    )
    .join("") + (state.payoutsHasMore
      ? '<button type="button" class="button button-quiet" data-action="load-more-payouts">Загрузить ещё выплаты</button>'
      : "");
}
export function renderPayouts() {
  renderPayoutsInto();
}
export function renderCurrentPayout() {
  const p = state.selectedPayoutDetail?.payout;
  const label = p ? `\u0422\u0435\u043a\u0443\u0449\u0430\u044f: #${state.selectedPayoutId} \u00b7 ${payoutPeriodLabel(p)} \u00b7 ${nameFor(PAYOUT_WORKFLOW_LABELS, p.workflow_state)} \u00b7 ${payoutProgress(p)}` : "";
  const history = $("history-current-context");
  if (history) history.innerHTML = p
    ? `<span>${escapeHtml(label)}</span><button type="button" class="button button-quiet" data-action="clear-payout">Снять выбор</button><button type="button" class="button button-quiet" data-action="delete-payout" ${p.status === "sending" ? 'disabled title="Дождитесь завершения рассылки"' : ""}>Удалить выплату</button>`
    : "";
  for (const id of ["selection-payout-context", "recipients-context"]) {
    const el = $(id);
    if (el) { el.textContent = label; el.classList.toggle("hidden", !p); }
  }
}
export function renderActionState() {
  const b = document.querySelector('[data-action="create-payout"]');
  if (b) {
    b.disabled = !canUseApi() || state.loading.createPayout;
  }
  const send = document.querySelector('[data-action="send-payout"]');
  const payoutIsDraft =
    state.selectedPayoutDetail?.payout?.status === "draft";
  const canSend =
    canUseApi() &&
    payoutIsDraft &&
    state.selectedUsers.size > 0 &&
    !state.loading.sendPayout;
  if (send) send.disabled = !canSend;
  $("selection-bar")?.classList.toggle(
    "hidden",
    state.activeMobileView !== "users" || !canSend,
  );
}
export function renderPreview() {
  const n = $("message-count");
  if (n)
    n.textContent = `${countComposerMessage(state.composer)} / ${MAX_PAYOUT_MESSAGE_LENGTH}`;
  const p = $("preview");
  if (p) p.textContent = composerMessageText(state.composer);
  const toggle = document.querySelector('[data-action="toggle-preview"]');
  if (toggle) toggle.setAttribute("aria-expanded", String(state.previewOpen));
}
export function renderNotifications() {
  const el = $("toast");
  if (!el) return;
  const n = state.notification;
  el.textContent = n?.message || "";
  el.className = n ? `toast toast-${n.kind}` : "toast hidden";
  el.setAttribute("role", n?.kind === "error" ? "alert" : "status");
}
