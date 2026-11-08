import { $, canUseApi, state } from "./store.js";
import { RECIPIENT_STATUS_LABELS } from "./constants.js";
import { escapeHtml } from "./utils.js";

const isProblem = (recipient) =>
  ["failed", "payment_required"].includes(recipient.status);

function statusClass(status) {
  if (status === "payment_received" || status === "paid")
    return "badge-success";
  if (["pending", "sending", "sent", "payment_required"].includes(status))
    return "badge-warning";
  if (status === "failed" || status === "cancelled") return "badge-danger";
  return "";
}

function paymentDetails(recipient) {
  const snapshot = recipient.payment_profile_snapshot;
  if (!snapshot) return "";
  if (typeof snapshot === "string") return snapshot;
  if (typeof snapshot.raw_payment_details === "string")
    return snapshot.raw_payment_details;
  return Object.entries(snapshot)
    .filter(([, value]) => value != null && value !== "")
    .map(([key, value]) => `${key}: ${value}`)
    .join("\n");
}

function visibleRecipients() {
  const filtered = state.recipients
    .map((recipient, index) => ({ recipient, index }))
    .filter(({ recipient }) => {
      if (state.recipientFilter === "payable")
        return recipient.status === "payment_received";
      if (state.recipientFilter === "paid") return recipient.status === "paid";
      if (state.recipientFilter === "problems") return isProblem(recipient);
      return true;
    });
  const priority = (status) => {
    if (["payment_required", "payment_received", "failed"].includes(status))
      return 0;
    if (["pending", "sending", "sent"].includes(status)) return 1;
    return 2;
  };
  filtered.sort(
    (a, b) =>
      priority(a.recipient.status) - priority(b.recipient.status) ||
      a.index - b.index,
  );
  return filtered.map(({ recipient }) => recipient);
}

function renderRow(recipient, payoutStatus) {
  const id = Number(recipient.id);
  const expanded = state.expandedRecipients.has(id);
  const details = paymentDetails(recipient);
  const canMarkPaid =
    recipient.status === "payment_received" &&
    !["draft", "sending", "cancelled"].includes(payoutStatus);
  const operationError = state.recipientOperationError[id];
  const failure =
    recipient.status === "failed" && recipient.failure_reason
      ? `<p>Ошибка отправки: ${escapeHtml(recipient.failure_reason)}</p>`
      : "";
  const paidNote =
    recipient.status === "paid" && recipient.paid_note
      ? `<p>Примечание: ${escapeHtml(recipient.paid_note)}</p>`
      : "";
  const noSnapshot =
    !details && ["payment_received", "paid"].includes(recipient.status)
      ? "<p>Снимок платёжных данных не сохранён.</p>"
      : "";
  return `<article class="recipient-row" data-recipient-row="${id}">
    <button type="button" class="recipient-summary-line" data-expand-recipient="${id}" aria-expanded="${expanded}">
      <span class="recipient-name">${escapeHtml(recipient.full_name || "")}</span>
      <span class="badge ${statusClass(recipient.status)}">${escapeHtml(RECIPIENT_STATUS_LABELS[recipient.status] || "Неизвестный статус")}</span>
      <span aria-hidden="true">${expanded ? "⌃" : "⌄"}</span>
    </button>
    <div class="recipient-details${expanded ? "" : " hidden"}" data-recipient-details="${id}">
      <strong class="recipient-detail-name">${escapeHtml(recipient.full_name || "")}</strong>
      ${operationError ? `<p class="field-error" role="alert">Не удалось отметить выплату: ${escapeHtml(operationError)}. Попробуйте ещё раз.</p>` : ""}
      ${details ? `<pre>${escapeHtml(details)}</pre><button type="button" class="button button-quiet" data-copy-recipient="${id}">Копировать</button>` : ""}
      ${failure}${paidNote}${noSnapshot}
      ${canMarkPaid ? `<button type="button" class="button button-quiet" data-action="mark-paid" data-recipient-id="${id}" ${state.loading.markPaid && state.loadingRecipientId === id ? "disabled" : ""}>Отметить выплаченным</button>` : ""}
    </div>
  </article>`;
}

export function renderRecipientsInto() {
  const root = $("recipients-list");
  if (!root) return;
  const scrollY = window.scrollY;
  const counts = {
    all: state.recipients.length,
    payable: state.recipients.filter(
      (recipient) => recipient.status === "payment_received",
    ).length,
    paid: state.recipients.filter((recipient) => recipient.status === "paid")
      .length,
    problems: state.recipients.filter(isProblem).length,
  };
  const summary = $("recipient-summary");
  const labels = {
    all: "Всего получателей",
    payable: "Данные подтверждены · к оплате",
    paid: "Выплачено",
    problems: "Проблемы",
  };
  if (summary) {
    summary.innerHTML = Object.entries(counts)
      .map(
        ([key, count]) =>
          `<div class="stat"><strong>${count}</strong><span>${labels[key]}</span></div>`,
      )
      .join("");
  }
  document.querySelectorAll("[data-filter]").forEach((button) => {
    const active = button.dataset.filter === state.recipientFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const stateLabel = $("recipients-state");
  if (stateLabel) {
    stateLabel.textContent =
      state.loading.recipients && !state.recipients.length
        ? "Загрузка получателей…"
        : state.recipientsLoadError
          ? `Не удалось загрузить получателей: ${state.recipientsLoadError}. Обновите раздел.`
          : !canUseApi() || !state.selectedPayoutId
            ? "Выберите выплату в истории."
            : !state.recipients.length
              ? "У этой выплаты пока нет получателей."
              : "";
  }
  const payoutStatus = state.selectedPayoutDetail?.payout?.status || "";
  root.innerHTML = visibleRecipients()
    .map((recipient) => renderRow(recipient, payoutStatus))
    .join("");
  if (state.activeMobileView === "recipients")
    window.scrollTo({ top: scrollY, behavior: "instant" });
}

export function renderRecipients() {
  renderRecipientsInto();
}
