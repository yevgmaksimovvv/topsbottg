import { RECIPIENT_STATUS_LABELS } from "./constants.js";
import { state } from "./store.js";
import { badgeClassForStatus, emptyStateMarkup, escapeHtml, loadingStateMarkup, statusLabel } from "./utils.js";
import { recipientsEmptyMessage, selectedPayoutSummaryText } from "./render-common.js";

function recipientPaymentSnapshotText(snapshot) {
  if (!snapshot) return "";
  if (typeof snapshot === "string") return snapshot.trim();
  if (typeof snapshot === "object" && snapshot !== null) {
    if (Object.prototype.hasOwnProperty.call(snapshot, "raw_payment_details")) {
      if (typeof snapshot.raw_payment_details === "string" && snapshot.raw_payment_details.trim()) {
        return snapshot.raw_payment_details.trim();
      }
      return "";
    }
    return JSON.stringify(snapshot);
  }
  return String(snapshot).trim();
}

function recipientPaymentBlock(recipient) {
  const snapshotText = recipientPaymentSnapshotText(recipient.payment_profile_snapshot);
  const confirmed = recipient.status === "payment_received" || recipient.status === "paid";
  if (snapshotText) {
    return `
      <div class="recipient-payment">
        <span class="recipient-payment-label">Платёжные данные:</span>
        <pre class="recipient-payment-value">${escapeHtml(snapshotText)}</pre>
      </div>`;
  }
  if (recipient.status === "failed") return "";
  if (confirmed) {
    return `<div class="status-note status-note-warning">Данные подтверждены, но снимок платёжных данных отсутствует</div>`;
  }
  return `<div class="status-note">Платёжные данные не подтверждены</div>`;
}

function recipientExtraNote(recipient) {
  if (recipient.status === "failed") {
    const reason = recipient.failure_reason ? `: ${escapeHtml(recipient.failure_reason)}` : "";
    return `<div class="status-note status-note-danger">Ошибка отправки${reason}</div>`;
  }
  if (recipient.status === "paid") {
    return recipient.paid_note ? `<div class="status-note">Примечание выплаты: ${escapeHtml(recipient.paid_note)}</div>` : "";
  }
  return "";
}

function canMarkPaid(recipient, payoutStatus) {
  return recipient.status === "payment_received" && !["draft", "sending", "cancelled"].includes(payoutStatus);
}

export function renderRecipientsInto(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return;
  if (state.loading.recipients && !state.recipients.length) {
    root.innerHTML = `
      <div class="recipient-summary">
        <div class="selected-summary">${escapeHtml(selectedPayoutSummaryText())}</div>
        ${loadingStateMarkup("Получатели", "Загрузка…")}
      </div>`;
    return;
  }
  const empty = recipientsEmptyMessage();
  const summary = selectedPayoutSummaryText();
  if (empty) {
    root.innerHTML = `
      <div class="recipient-summary">
        <div class="selected-summary">${escapeHtml(summary)}</div>
        ${emptyStateMarkup("Получатели", empty)}
      </div>`;
    return;
  }
  const payoutStatus = state.selectedPayoutDetail?.payout?.status || "";
  root.innerHTML = `
    <div class="recipient-summary">
      <div class="selected-summary">${escapeHtml(summary)}</div>
      <div class="recipient-list">
        ${state.recipients
          .map((recipient) => {
            const markPaidButton = canMarkPaid(recipient, payoutStatus)
              ? `<button type="button" class="secondary-button" data-action="mark-paid" data-recipient-id="${recipient.id}" ${
                  state.loading.markPaid && state.loadingRecipientId === recipient.id ? "disabled" : ""
                }>${state.loading.markPaid && state.loadingRecipientId === recipient.id ? '<span class="spinner"></span> Отмечаем…' : "Отметить выплаченным"}</button>`
              : "";
            return `
              <article class="recipient-card">
                <div class="user-line">
                  <strong>${escapeHtml(recipient.full_name)}</strong>
                  <span class="badge ${badgeClassForStatus(recipient.status)}">${escapeHtml(statusLabel(
                    RECIPIENT_STATUS_LABELS,
                    recipient.status
                  ))}</span>
                </div>
                ${recipientPaymentBlock(recipient)}
                ${recipientExtraNote(recipient)}
                ${markPaidButton ? `<div class="recipient-actions">${markPaidButton}</div>` : ""}
              </article>`;
          })
          .join("")}
      </div>
    </div>`;
}

export function renderRecipients() {
  renderRecipientsInto("desktop-recipients");
  renderRecipientsInto("mobile-recipients");
}
