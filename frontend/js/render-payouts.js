import { PAYOUT_STATUS_LABELS } from "./constants.js";
import { state } from "./store.js";
import { emptyStateMarkup, escapeHtml, statusLabel } from "./utils.js";
import { payoutsEmptyMessage } from "./render-common.js";

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
      return `
        <button type="button" class="payout-card ${selected ? "selected" : ""}" data-payout-id="${payout.id}">
          <div class="payout-main">
            <strong>#${payout.id} · ${escapeHtml(payout.title)}</strong>
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
