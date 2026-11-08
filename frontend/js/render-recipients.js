import { $, canUseApi, state } from "./store.js";
import { RECIPIENT_STATUS_LABELS } from "./constants.js";
import { escapeHtml, formatTime } from "./utils.js";

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
    !["draft", "sending", "cancelled"].includes(payoutStatus) &&
    !state.recipientRefreshRequired[id];
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
      ${Number(recipient.payment_snapshot_version || 0) > 1 ? `<p class="snapshot-updated">\u0420\u0435\u043a\u0432\u0438\u0437\u0438\u0442\u044b \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u044b${recipient.payment_snapshot_updated_at ? ` \u00b7 ${escapeHtml(formatTime(recipient.payment_snapshot_updated_at))}` : ""}</p>` : ""}
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
  const labels = { all: "\u0412\u0441\u0435\u0433\u043e \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u0435\u0439", payable: "\u0413\u043e\u0442\u043e\u0432 \u043a \u043e\u043f\u043b\u0430\u0442\u0435", paid: "\u0412\u044b\u043f\u043b\u0430\u0447\u0435\u043d\u043e", problems: "\u041f\u0440\u043e\u0431\u043b\u0435\u043c\u044b" };
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
  const noPayout = !state.selectedPayoutId;
  $("recipient-summary")?.classList.toggle("hidden", noPayout);
  $("recipient-filters")?.classList.toggle("hidden", noPayout);
  root.classList.toggle("hidden", noPayout);
  if (stateLabel) {
    if (noPayout) {
      stateLabel.innerHTML = "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0432\u044b\u043f\u043b\u0430\u0442\u0443 \u0432 \u0438\u0441\u0442\u043e\u0440\u0438\u0438 <button type=\"button\" class=\"button button-quiet\" data-action=\"open-history\">\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u0438\u0441\u0442\u043e\u0440\u0438\u044e</button>";
    } else {
      stateLabel.textContent = state.loading.recipients && !state.recipients.length
        ? "\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u0435\u0439\u2026"
        : state.recipientsLoadError
          ? `\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u0435\u0439: ${state.recipientsLoadError}. \u041e\u0431\u043d\u043e\u0432\u0438\u0442\u0435 \u0440\u0430\u0437\u0434\u0435\u043b.`
          : !state.recipients.length
            ? "\u0423 \u044d\u0442\u043e\u0439 \u0432\u044b\u043f\u043b\u0430\u0442\u044b \u043f\u043e\u043a\u0430 \u043d\u0435\u0442 \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u0435\u0439."
            : "";
    }
  }
  const payoutStatus = state.selectedPayoutDetail?.payout?.status || "";
  const payoutId = String(state.selectedPayoutId ?? "");
  const filter = state.recipientFilter;
  const rebuild =
    root.dataset.renderPayoutId !== payoutId ||
    root.dataset.renderFilter !== filter;
  if (rebuild) root.replaceChildren();

  const recipients = visibleRecipients();
  const wanted = new Set(recipients.map((recipient) => String(Number(recipient.id))));
  for (const row of [...root.querySelectorAll(":scope > [data-recipient-row]")]) {
    if (!wanted.has(row.dataset.recipientRow)) row.remove();
  }

  recipients.forEach((recipient, index) => {
    const id = String(Number(recipient.id));
    let row = root.querySelector(`[data-recipient-row="${id}"]`);
    const markup = renderRow(recipient, payoutStatus);
    const signature = JSON.stringify({
      recipient,
      payoutStatus,
      expanded: state.expandedRecipients.has(Number(id)),
      operationError: state.recipientOperationError[id] || "",
      markPaidLoading:
        state.loading.markPaid && state.loadingRecipientId === Number(id),
    });
    if (!row) {
      const template = document.createElement("template");
      template.innerHTML = markup;
      row = template.content.firstElementChild;
      root.append(row);
    } else if (row.dataset.renderSignature !== signature) {
      const active = row.contains(document.activeElement)
        ? document.activeElement
        : null;
      const focusKey = active
        ? [...active.attributes]
            .filter((attribute) => attribute.name.startsWith("data-"))
            .map((attribute) => [attribute.name, attribute.value])
        : [];
      const selection =
        active && "selectionStart" in active
          ? [active.selectionStart, active.selectionEnd]
          : null;
      row.innerHTML = markup.slice(markup.indexOf(">") + 1, markup.lastIndexOf("</article>"));
      if (focusKey.length) {
        const selector = focusKey
          .map(([name, value]) => `[${name}="${CSS.escape(value)}"]`)
          .join("");
        const replacement = row.querySelector(selector);
        replacement?.focus();
        if (selection && replacement && "setSelectionRange" in replacement)
          replacement.setSelectionRange(...selection);
      }
    }
    row.querySelector("[data-expand-recipient]")?.setAttribute(
      "aria-expanded",
      String(state.expandedRecipients.has(Number(id))),
    );
    row.dataset.renderSignature = signature;
    const current = root.children[index];
    if (current !== row) root.insertBefore(row, current || null);
  });
  root.dataset.renderPayoutId = payoutId;
  root.dataset.renderFilter = filter;
  if (state.activeMobileView === "recipients" && window.scrollY !== scrollY)
    window.scrollTo({ top: scrollY, behavior: "instant" });
}

export function renderRecipients() {
  renderRecipientsInto();
}
