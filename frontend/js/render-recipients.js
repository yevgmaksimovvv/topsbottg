import { RECIPIENT_STATUS_LABELS } from "./constants.js";
import { state } from "./store.js";
import { badgeClassForStatus, emptyStateMarkup, escapeHtml, loadingStateMarkup, statusLabel } from "./utils.js";
import { recipientsEmptyMessage, selectedPayoutSummaryText } from "./render-common.js";

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
  root.innerHTML = `
    <div class="recipient-summary">
      <div class="selected-summary">${escapeHtml(summary)}</div>
      <div class="recipient-list">
        ${state.recipients
          .map((recipient) => {
            const reply = recipient.reply?.raw_text ? recipient.reply.raw_text : "Ответ не получен";
            const markPaidButton =
              recipient.status === "payment_received"
                ? `<button type="button" class="secondary-button" data-action="mark-paid" data-recipient-id="${recipient.id}" ${
                    state.loading.markPaid && state.loadingRecipientId === recipient.id ? "disabled" : ""
                  }>${state.loading.markPaid && state.loadingRecipientId === recipient.id ? '<span class="spinner"></span> Отмечаем…' : "Отметить выплаченным"}</button>`
                : "";
            return `
              <article class="recipient-card">
                <div class="user-line">
                  <strong>#${recipient.id} · ${escapeHtml(recipient.full_name)}</strong>
                  <span class="badge ${badgeClassForStatus(recipient.status)}">${statusLabel(
                    RECIPIENT_STATUS_LABELS,
                    recipient.status
                  )}</span>
                </div>
                <div class="meta">Telegram ID: ${escapeHtml(recipient.telegram_user_id || recipient.telegram_id)}</div>
                ${
                  recipient.paid_note
                    ? `<div class="status-note">${escapeHtml(recipient.paid_note)}</div>`
                    : recipient.failure_reason
                      ? `<div class="status-note">${escapeHtml(recipient.failure_reason)}</div>`
                      : ""
                }
                <div class="recipient-actions">
                  ${markPaidButton}
                  <details class="reply-details">
                    <summary>Ответ</summary>
                    <pre>${escapeHtml(reply)}</pre>
                  </details>
                </div>
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
