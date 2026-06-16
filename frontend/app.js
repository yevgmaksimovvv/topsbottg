const PAYOUT_STATUS_LABELS = {
  draft: "черновик",
  sending: "идет рассылка",
  sent: "отправлено",
  partially_failed: "часть отправок не удалась",
  completed: "закрыта админом",
  cancelled: "отменена",
};

const RECIPIENT_STATUS_LABELS = {
  pending: "ожидает отправки",
  sending: "отправляется",
  sent: "сообщение отправлено",
  failed: "ошибка отправки",
  payment_required: "нужны данные",
  payment_received: "данные получены",
  paid: "выплачено",
  cancelled: "исключен из выплаты",
};

const AUTO_HIDE_TOAST_MS = 3200;
const USERS_LIMIT = 50;
const SEARCH_DEBOUNCE_MS = 300;
const POLL_INTERVAL_MS = 4000;
const MOBILE_QUERY = window.matchMedia("(max-width: 759px)");

const COMPOSER_IDS = {
  desktop: {
    title: "desktop-payout-title",
    periodFrom: "desktop-period-from",
    periodTo: "desktop-period-to",
    messageTemplate: "desktop-message-template",
    validation: "desktop-payout-validation",
    preview: "desktop-preview",
  },
  mobile: {
    title: "mobile-payout-title",
    periodFrom: "mobile-period-from",
    periodTo: "mobile-period-to",
    messageTemplate: "mobile-message-template",
    validation: "mobile-payout-validation",
    preview: "mobile-preview",
  },
};

const state = {
  initData: window.Telegram?.WebApp?.initData || "",
  activeMobileView: "users",
  users: [],
  payouts: [],
  recipients: [],
  selectedUsers: new Set(),
  selectedPayoutId: null,
  selectedPayoutDetail: null,
  composer: {
    title: "",
    periodFrom: "",
    periodTo: "",
    messageTemplate: "",
  },
  usersLimit: USERS_LIMIT,
  usersOffset: 0,
  usersHasMore: false,
  usersSearch: "",
  usersHasPayment: "",
  usersIsActive: "",
  loading: {
    users: false,
    payouts: false,
    recipients: false,
    createPayout: false,
    attachSelected: false,
    sendPayout: false,
    exportCsv: false,
    markPaid: false,
    revealPaymentDetails: false,
  },
  loadingUserId: null,
  loadingRecipientId: null,
  error: null,
  toast: null,
  pollingTimer: null,
  usersRequestId: 0,
  payoutRequestId: 0,
  modalPaymentDetails: null,
  toastTimer: null,
  searchTimer: null,
};

const $ = (id) => document.getElementById(id);

function telegramWebApp() {
  return window.Telegram?.WebApp || null;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function statusLabel(map, value) {
  return map[value] || value;
}

function badgeClassForStatus(status) {
  if (["paid", "sent"].includes(status)) return "badge-success";
  if (["sending", "payment_received"].includes(status)) return "badge-warning";
  if (["failed", "cancelled", "partially_failed"].includes(status)) return "badge-danger";
  return "badge-muted";
}

function initials(name) {
  const parts = String(name)
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2);
  if (!parts.length) return "TB";
  return parts.map((part) => part[0].toUpperCase()).join("");
}

function formatTime(value) {
  const date = value ? new Date(value) : new Date();
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function canUseApi() {
  return Boolean(state.initData);
}

function getComposerElements() {
  const side = MOBILE_QUERY.matches ? "mobile" : "desktop";
  const ids = COMPOSER_IDS[side];
  return {
    side,
    title: $(ids.title),
    periodFrom: $(ids.periodFrom),
    periodTo: $(ids.periodTo),
    messageTemplate: $(ids.messageTemplate),
    validation: $(ids.validation),
    preview: $(ids.preview),
  };
}

function getAllComposerElementPairs() {
  return Object.values(COMPOSER_IDS).map((ids) => ({
    title: $(ids.title),
    periodFrom: $(ids.periodFrom),
    periodTo: $(ids.periodTo),
    messageTemplate: $(ids.messageTemplate),
    validation: $(ids.validation),
    preview: $(ids.preview),
  }));
}

function setThemeVariables() {
  const theme = telegramWebApp()?.themeParams || {};
  const root = document.documentElement.style;
  root.setProperty("--bg", theme.bg_color || "#0e1621");
  root.setProperty("--surface", theme.secondary_bg_color || "#17212b");
  root.setProperty("--surface-2", theme.section_bg_color || "#1f2c38");
  root.setProperty("--surface-3", "#233445");
  root.setProperty("--text", theme.text_color || "#f3f7fb");
  root.setProperty("--muted", theme.hint_color || "#8ea2b5");
  root.setProperty("--primary", theme.button_color || "#2ea6ff");
  root.setProperty("--primary-contrast", theme.button_text_color || "#06111a");
  root.setProperty("--success", "#35c779");
  root.setProperty("--warning", "#f2b84b");
  root.setProperty("--danger", theme.destructive_text_color || "#ff5c7a");
  root.setProperty("--border", "rgba(255,255,255,.08)");
  root.setProperty("--radius", "18px");
  root.setProperty("--shadow", "0 16px 44px rgba(0,0,0,.28)");
}

function renderNotifications() {
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

function renderAuthState() {
  const pill = $("auth-state");
  const banner = $("auth-banner");
  if (!pill || !banner) return;
  if (canUseApi()) {
    pill.textContent = "Telegram подключён";
    pill.className = "auth-pill auth-ok";
    pill.classList.remove("hidden");
    banner.classList.add("hidden");
    banner.textContent = "";
  } else {
    pill.textContent = "";
    pill.className = "auth-pill hidden";
    banner.textContent = "Откройте через Telegram, чтобы загрузить данные.";
    banner.classList.remove("hidden");
  }
}

function setTextAll(ids, text) {
  ids.forEach((id) => {
    const node = $(id);
    if (node) node.textContent = text;
  });
}

function renderSelectedCount() {
  setTextAll(["selected-count", "selected-count-mobile"], String(state.selectedUsers.size));
}

function setButtonState(action, { label, loading = false, disabled = false, hint = "" }) {
  document.querySelectorAll(`[data-action="${action}"]`).forEach((button) => {
    button.disabled = disabled;
    button.title = hint;
    button.innerHTML = loading ? `<span class="spinner"></span> ${escapeHtml(label)}` : escapeHtml(label);
  });
}

function renderMobileView() {
  document.querySelectorAll("[data-mobile-view]").forEach((button) => {
    const active = button.dataset.mobileView === state.activeMobileView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  document.querySelectorAll("[data-mobile-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.mobilePanel !== state.activeMobileView);
  });
}

function setMobileView(view) {
  state.activeMobileView = view;
  renderMobileView();
}

function emptyStateMarkup(title, text) {
  return `
    <div class="empty-state">
      <div class="empty-state-dot"></div>
      <div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(text)}</p>
      </div>
    </div>`;
}

function formatPayoutPreviewText() {
  const payoutDetail = state.selectedPayoutDetail?.payout || null;
  const template =
    state.composer.messageTemplate.trim() ||
    payoutDetail?.message_template ||
    "";
  if (!template.trim()) return "";
  const periodFrom = state.composer.periodFrom || payoutDetail?.period_from || "";
  const periodTo = state.composer.periodTo || payoutDetail?.period_to || "";
  return template.replaceAll("{period_from}", periodFrom).replaceAll("{period_to}", periodTo);
}

function renderPreviewInto(rootId) {
  const root = $(rootId);
  if (!root) return;
  const text = formatPayoutPreviewText();
  if (!text) {
    root.innerHTML = `
      <div class="preview-empty">
        <strong>Предпросмотр</strong>
        <p>Введите шаблон, чтобы увидеть Telegram-представление сообщения.</p>
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

function renderPreview() {
  renderPreviewInto("desktop-preview");
  renderPreviewInto("mobile-preview");
}

function usersEmptyMessage() {
  if (!canUseApi()) return "Пользователи появятся после открытия через Telegram.";
  if (state.loading.users) return "Загрузка пользователей...";
  if (!state.users.length) return "Пользователи не найдены.";
  return "";
}

function payoutsEmptyMessage() {
  if (!canUseApi()) return "Выплаты появятся после открытия через Telegram.";
  if (state.loading.payouts) return "Загрузка выплат...";
  if (!state.payouts.length) return "Выплат пока нет.";
  return "";
}

function recipientsEmptyMessage() {
  if (!canUseApi()) return "Выберите выплату, чтобы увидеть получателей.";
  if (state.loading.recipients) return "Загрузка получателей...";
  if (!state.selectedPayoutId) return "Выберите выплату.";
  if (!state.recipients.length) return "У этой выплаты пока нет получателей.";
  return "";
}

function selectedPayoutSummaryText() {
  if (!state.selectedPayoutDetail) return "Выплата не выбрана";
  const { payout } = state.selectedPayoutDetail;
  return `#${payout.id} · ${payout.title} · ${statusLabel(PAYOUT_STATUS_LABELS, payout.status)} · ${state.recipients.length} получ.`;
}

function renderUsersInto(rootId) {
  const root = $(rootId);
  if (!root) return;
  const empty = usersEmptyMessage();
  if (empty) {
    root.innerHTML = emptyStateMarkup("Пользователи", empty);
    return;
  }
  root.innerHTML = state.users
    .map((user) => {
      const revealLoading = state.loading.revealPaymentDetails && state.loadingUserId === user.id;
      const revealDisabled = !user.has_payment_profile || state.loading.revealPaymentDetails || !canUseApi();
      return `
        <article class="user-card">
          <div class="avatar" aria-hidden="true">${escapeHtml(initials(user.full_name))}</div>
          <div class="user-main">
            <div class="user-line">
              <strong>${escapeHtml(user.full_name)}</strong>
              <span class="badge ${user.is_active ? "badge-success" : "badge-muted"}">${
                user.is_active ? "Активен" : "Неактивен"
              }</span>
              <span class="badge ${user.has_payment_profile ? "badge-warning" : "badge-muted"}">${
                user.has_payment_profile ? "Есть данные" : "Нет данных"
              }</span>
            </div>
            <div class="meta">Telegram ID: ${escapeHtml(user.telegram_user_id || user.telegram_id)}</div>
          </div>
          <div class="user-actions">
            <label class="checkbox-cell" aria-label="Выбрать пользователя">
              <input type="checkbox" data-user-id="${user.id}" ${state.selectedUsers.has(user.id) ? "checked" : ""} ${
                !canUseApi() ? "disabled" : ""
              } />
            </label>
            ${
              canUseApi() && user.has_payment_profile
                ? `<button type="button" class="secondary-button" data-action="reveal-payment" data-user-id="${user.id}" ${
                    revealDisabled ? "disabled" : ""
                  }>${revealLoading ? '<span class="spinner"></span> Открываем…' : "Показать"}</button>`
                : `<span class="meta">${user.has_payment_profile ? "Есть данные" : "Нет данных"}</span>`
            }
          </div>
        </article>`;
    })
    .join("");

  root.querySelectorAll('input[type="checkbox"][data-user-id]').forEach((input) => {
    input.addEventListener("change", (event) => {
      const id = Number(event.target.dataset.userId);
      if (event.target.checked) {
        state.selectedUsers.add(id);
      } else {
        state.selectedUsers.delete(id);
      }
      renderSelectedCount();
      renderSelectionBars();
    });
  });

  root.querySelectorAll('[data-action="reveal-payment"]').forEach((button) => {
    button.addEventListener("click", () => revealPaymentDetails(Number(button.dataset.userId)));
  });
}

function renderUsers() {
  renderUsersInto("desktop-users");
  renderUsersInto("mobile-users");
}

function renderSelectionBarInto(rootId) {
  const root = $(rootId);
  if (!root) return;
  if (!state.selectedUsers.size) {
    root.classList.add("hidden");
    root.innerHTML = "";
    return;
  }
  root.classList.remove("hidden");
  root.innerHTML = `
    <div class="selection-bar-copy">
      <div class="selected-counter">
        <span>Выбрано</span>
        <strong>${state.selectedUsers.size}</strong>
      </div>
      <span>${state.selectedPayoutId ? "Добавьте выбранных пользователей в текущую выплату." : "Выберите выплату для добавления."}</span>
    </div>
    <button type="button" class="secondary-button" data-action="attach-selected">Добавить выбранных</button>`;
}

function renderSelectionBars() {
  renderSelectionBarInto("desktop-selection-bar");
  renderSelectionBarInto("mobile-selection-bar");
}

function renderPayoutsInto(rootId) {
  const root = $(rootId);
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

  root.querySelectorAll("[data-payout-id]").forEach((button) => {
    button.addEventListener("click", () => selectPayout(Number(button.dataset.payoutId)));
  });
}

function renderPayouts() {
  renderPayoutsInto("desktop-payouts");
  renderPayoutsInto("mobile-payouts");
}

function renderCurrentPayoutInto(rootId) {
  const root = $(rootId);
  if (!root) return;
  if (!canUseApi()) {
    root.innerHTML = emptyStateMarkup("Текущая выплата", "Выберите выплату.");
    return;
  }
  if (!state.selectedPayoutDetail) {
    root.innerHTML = emptyStateMarkup("Текущая выплата", "Выберите выплату.");
    return;
  }
  const payout = state.selectedPayoutDetail.payout;
  const actionsVisible = Boolean(state.selectedPayoutId);
  root.innerHTML = `
    <div class="current-payout-card">
      <div class="current-payout-head">
        <div>
          <strong>#${payout.id} · ${escapeHtml(payout.title)}</strong>
          <div class="meta">${escapeHtml(statusLabel(PAYOUT_STATUS_LABELS, payout.status))}</div>
        </div>
        <span class="badge ${badgeClassForStatus(payout.status)}">${escapeHtml(statusLabel(PAYOUT_STATUS_LABELS, payout.status))}</span>
      </div>
      <div class="current-payout-stats">
        <span>${state.recipients.length} получателей</span>
      </div>
      ${
        actionsVisible
          ? `
        <div class="current-payout-actions">
          <button type="button" data-action="send-payout">Разослать</button>
          <button type="button" class="secondary-button" data-action="export-csv">Скачать CSV</button>
        </div>`
          : ""
      }
    </div>`;
}

function renderCurrentPayout() {
  renderCurrentPayoutInto("desktop-current-payout");
  renderCurrentPayoutInto("mobile-current-payout");
}

function renderRecipientsInto(rootId) {
  const root = $(rootId);
  if (!root) return;
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
                ? `<button type="button" class="secondary-button" data-action="mark-paid" data-recipient-id="${
                    recipient.id
                  }" ${
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

  root.querySelectorAll('[data-action="mark-paid"]').forEach((button) => {
    button.addEventListener("click", () => markPaid(Number(button.dataset.recipientId)));
  });
}

function renderRecipients() {
  renderRecipientsInto("desktop-recipients");
  renderRecipientsInto("mobile-recipients");
}

function renderActionState() {
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
  setButtonState("attach-selected", {
    label: state.loading.attachSelected ? "Добавляем…" : "Добавить выбранных",
    loading: state.loading.attachSelected,
    disabled: state.loading.attachSelected || !state.selectedPayoutId || !state.selectedUsers.size || !canUseApi(),
    hint:
      !canUseApi()
        ? "Нужен доступ к данным."
        : state.selectedPayoutId && state.selectedUsers.size
          ? "Добавить выбранных пользователей в текущую выплату."
          : "Нужна выбранная выплата и хотя бы один пользователь.",
  });
  document.querySelectorAll('[data-action="attach-selected"]').forEach((button) => {
    button.classList.toggle("hidden", !canUseApi() || !state.selectedUsers.size || !state.selectedPayoutId);
  });
  setButtonState("copy-payment-details", {
    label: "Скопировать",
    disabled: !state.modalPaymentDetails || !canUseApi(),
    hint: state.modalPaymentDetails ? "Скопировать данные." : "Нет данных для копирования.",
  });
  setButtonState("close-payment-modal", {
    label: "×",
    disabled: false,
    hint: "Закрыть окно платёжных данных.",
  });
  setButtonState("close-payment-modal-secondary", {
    label: "Закрыть",
    disabled: false,
    hint: "Закрыть окно платёжных данных.",
  });
}

function syncComposerFields() {
  getAllComposerElementPairs().forEach((fields) => {
    if (fields.title) fields.title.value = state.composer.title;
    if (fields.periodFrom) fields.periodFrom.value = state.composer.periodFrom;
    if (fields.periodTo) fields.periodTo.value = state.composer.periodTo;
    if (fields.messageTemplate) fields.messageTemplate.value = state.composer.messageTemplate;
    if (fields.validation) {
      fields.validation.textContent = "";
      fields.validation.classList.add("hidden");
    }
  });
  renderPreview();
}

function validatePayoutForm() {
  const errors = [];
  if (!state.composer.title.trim()) errors.push("Введите название выплаты.");
  if (!state.composer.periodFrom) errors.push("Укажите дату начала периода.");
  if (!state.composer.periodTo) errors.push("Укажите дату окончания периода.");
  if (state.composer.periodFrom && state.composer.periodTo && state.composer.periodFrom > state.composer.periodTo) {
    errors.push("Период начала не может быть позже периода окончания.");
  }

  getAllComposerElementPairs().forEach((fields) => {
    if (!fields.validation) return;
    fields.validation.textContent = errors.join(" ");
    fields.validation.classList.toggle("hidden", errors.length === 0);
  });

  return errors.length === 0 ? { ...state.composer } : null;
}

function renderModal() {
  const modal = $("payment-modal");
  const title = $("payment-modal-title");
  const subtitle = $("payment-modal-subtitle");
  const body = $("payment-modal-body");
  const closeButton = $("close-payment-modal");
  if (!modal || !title || !subtitle || !body || !closeButton) return;
  if (!state.modalPaymentDetails) {
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    title.textContent = "";
    subtitle.textContent = "";
    body.textContent = "";
    document.body.classList.remove("modal-open");
    return;
  }
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  title.textContent = state.modalPaymentDetails.full_name || "Платёжные данные";
  subtitle.textContent = `Telegram ID: ${state.modalPaymentDetails.telegram_user_id || state.modalPaymentDetails.telegram_id || "—"}`;
  body.textContent = state.modalPaymentDetails.raw_payment_details || "";
  document.body.classList.add("modal-open");
  window.requestAnimationFrame(() => closeButton.focus());
}

function renderUI() {
  renderNotifications();
  renderAuthState();
  renderSelectedCount();
  renderSelectionBars();
  renderPreview();
  renderUsers();
  renderCurrentPayout();
  renderPayouts();
  renderRecipients();
  renderActionState();
  renderModal();
  renderMobileView();
}

async function readErrorMessage(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const data = await response.json();
    return data.detail || JSON.stringify(data);
  }
  return response.text();
}

async function api(path, options = {}) {
  if (!canUseApi()) {
    throw new Error("Нет доступа к данным");
  }
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      ...(options.headers || {}),
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": state.initData,
    },
  });
  if (!response.ok) {
    throw new Error(await readErrorMessage(response));
  }
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return response.json();
  return response.text();
}

function setLoading(key, value) {
  state.loading[key] = value;
  renderActionState();
}

function setToast(message) {
  state.toast = message || null;
  if (state.toastTimer) {
    clearTimeout(state.toastTimer);
  }
  renderNotifications();
  if (state.toast) {
    state.toastTimer = window.setTimeout(() => {
      state.toast = null;
      renderNotifications();
    }, AUTO_HIDE_TOAST_MS);
  }
}

function setError(message) {
  state.error = message || null;
  renderNotifications();
}

function clearError() {
  state.error = null;
  renderNotifications();
}

async function safeApiAction(key, fn, { successMessage = null } = {}) {
  if (state.loading[key]) return null;
  setLoading(key, true);
  clearError();
  try {
    const result = await fn();
    if (successMessage) setToast(successMessage);
    return result;
  } catch (error) {
    setError(error.message || "Ошибка");
    return null;
  } finally {
    setLoading(key, false);
  }
}

async function loadUsers({ reset = true } = {}) {
  if (!canUseApi()) return;
  const requestId = ++state.usersRequestId;
  if (reset) {
    state.usersOffset = 0;
    state.users = [];
    state.usersHasMore = false;
  }
  setLoading("users", true);
  clearError();
  try {
    const params = new URLSearchParams();
    params.set("limit", String(state.usersLimit));
    params.set("offset", String(state.usersOffset));
    if (state.usersSearch.trim()) params.set("search", state.usersSearch.trim());
    if (state.usersHasPayment !== "") params.set("has_payment_profile", state.usersHasPayment);
    if (state.usersIsActive !== "") params.set("is_active", state.usersIsActive);
    const page = await api(`/admin/users?${params.toString()}`);
    if (requestId !== state.usersRequestId) return;
    state.users = reset ? page.items : [...state.users, ...page.items];
    state.usersHasMore = page.has_more;
    state.usersOffset = page.offset + page.items.length;
  } catch (error) {
    if (requestId === state.usersRequestId) {
      setError(error.message || "Не удалось загрузить пользователей");
    }
  } finally {
    if (requestId === state.usersRequestId) {
      setLoading("users", false);
      renderUI();
    }
  }
}

async function loadPayouts() {
  if (!canUseApi()) return;
  const requestId = ++state.payoutRequestId;
  setLoading("payouts", true);
  clearError();
  try {
    const payouts = await api("/admin/payouts");
    if (requestId !== state.payoutRequestId) return;
    state.payouts = payouts;
  } catch (error) {
    if (requestId === state.payoutRequestId) {
      setError(error.message || "Не удалось загрузить выплаты");
    }
  } finally {
    if (requestId === state.payoutRequestId) {
      setLoading("payouts", false);
      renderUI();
    }
  }
}

function stopPolling() {
  if (state.pollingTimer) {
    clearInterval(state.pollingTimer);
    state.pollingTimer = null;
  }
}

function startPollingSelectedPayout() {
  stopPolling();
  if (!state.selectedPayoutDetail || state.selectedPayoutDetail.payout.status !== "sending") return;
  state.pollingTimer = window.setInterval(() => {
    if (state.selectedPayoutId && !state.loading.recipients) {
      refreshSelectedPayout({ silent: true });
    }
  }, POLL_INTERVAL_MS);
}

async function refreshSelectedPayout({ silent = false } = {}) {
  if (!state.selectedPayoutId || !canUseApi()) return;
  const requestId = ++state.payoutRequestId;
  if (!silent) setLoading("recipients", true);
  try {
    const detail = await api(`/admin/payouts/${state.selectedPayoutId}`);
    const recipients = await api(`/admin/payouts/${state.selectedPayoutId}/recipients`);
    if (requestId !== state.payoutRequestId) return;
    state.selectedPayoutDetail = detail;
    state.recipients = recipients;
    renderUI();
    startPollingSelectedPayout();
  } catch (error) {
    if (!silent) setError(error.message || "Не удалось обновить выплату");
  } finally {
    if (!silent) {
      setLoading("recipients", false);
    }
  }
}

async function selectPayout(id) {
  if (!canUseApi()) return;
  state.selectedPayoutId = id;
  stopPolling();
  await safeApiAction("recipients", async () => {
    const detail = await api(`/admin/payouts/${id}`);
    const recipients = await api(`/admin/payouts/${id}/recipients`);
    state.selectedPayoutDetail = detail;
    state.recipients = recipients;
    startPollingSelectedPayout();
  });
  renderUI();
}

async function createPayout() {
  if (!canUseApi()) return;
  const payload = validatePayoutForm();
  if (!payload) return;
  if (!state.selectedUsers.size) {
    const confirmed = window.confirm("Создать выплату без получателей?");
    if (!confirmed) return;
  }
  await safeApiAction("createPayout", async () => {
    const payout = await api("/admin/payouts", {
      method: "POST",
      body: JSON.stringify({
        title: payload.title,
        period_from: payload.periodFrom,
        period_to: payload.periodTo,
        message_template: payload.messageTemplate.trim() || null,
      }),
    });
    if (state.selectedUsers.size) {
      await api(`/admin/payouts/${payout.id}/recipients`, {
        method: "POST",
        body: JSON.stringify({ user_ids: [...state.selectedUsers] }),
      });
    }
    setToast(`Выплата #${payout.id} создана.`);
    await loadPayouts();
    await selectPayout(payout.id);
  });
}

async function attachSelected() {
  if (!canUseApi() || !state.selectedPayoutId || !state.selectedUsers.size) return;
  await safeApiAction("attachSelected", async () => {
    await api(`/admin/payouts/${state.selectedPayoutId}/recipients`, {
      method: "POST",
      body: JSON.stringify({ user_ids: [...state.selectedUsers] }),
    });
    setToast("Выбранные пользователи добавлены к выплате.");
    await refreshSelectedPayout();
    await loadPayouts();
  });
}

async function sendPayout() {
  if (!canUseApi() || !state.selectedPayoutId) return;
  if (!state.selectedPayoutDetail || state.selectedPayoutDetail.payout.id !== state.selectedPayoutId) {
    await refreshSelectedPayout({ silent: true });
  }
  const payout = state.selectedPayoutDetail?.payout;
  const count = state.recipients.length;
  const confirmed = window.confirm(
    `Запустить рассылку выплаты "${payout?.title || state.selectedPayoutId}" для ${count} получателей?`
  );
  if (!confirmed) return;
  await safeApiAction("sendPayout", async () => {
    await api(`/admin/payouts/${state.selectedPayoutId}/send`, { method: "POST" });
    setToast("Рассылка запущена.");
    await refreshSelectedPayout();
    await loadPayouts();
  });
}

async function exportCsv() {
  if (!canUseApi() || !state.selectedPayoutId) return;
  await safeApiAction("exportCsv", async () => {
    const response = await fetch(`/api/admin/payouts/${state.selectedPayoutId}/export.csv`, {
      headers: {
        "X-Telegram-Init-Data": state.initData,
      },
    });
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `payout-${state.selectedPayoutId}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setToast("CSV выгружен.");
  });
}

async function markPaid(recipientId) {
  if (!canUseApi() || !state.selectedPayoutId) return;
  const confirmed = window.confirm("Вы уверены, что выплата этому получателю выполнена?");
  if (!confirmed) return;
  await safeApiAction("markPaid", async () => {
    state.loadingRecipientId = recipientId;
    renderRecipients();
    renderActionState();
    await api(`/admin/payouts/${state.selectedPayoutId}/recipients/${recipientId}/mark-paid`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    setToast("Получатель отмечен как выплаченный.");
    await refreshSelectedPayout();
    await loadPayouts();
  });
  state.loadingRecipientId = null;
  renderRecipients();
  renderActionState();
}

function openPaymentModal(details) {
  state.modalPaymentDetails = details;
  renderModal();
  renderActionState();
}

function closePaymentModal() {
  state.modalPaymentDetails = null;
  renderModal();
  renderActionState();
}

async function revealPaymentDetails(userId) {
  if (!canUseApi()) return;
  await safeApiAction("revealPaymentDetails", async () => {
    state.loadingUserId = userId;
    renderUsers();
    const details = await api(`/admin/users/${userId}/payment-details`);
    openPaymentModal(details);
  });
  state.loadingUserId = null;
  renderUsers();
}

async function copyPaymentDetails() {
  if (!state.modalPaymentDetails) return;
  try {
    await navigator.clipboard.writeText(state.modalPaymentDetails.raw_payment_details);
    setToast("Платёжные данные скопированы.");
  } catch {
    setError("Не удалось скопировать платёжные данные.");
  }
}

function syncComposerInput(key, value) {
  state.composer[key] = value;
  syncComposerFields();
}

function bindComposerInputs() {
  Object.entries(COMPOSER_IDS).forEach(([side, ids]) => {
    const title = $(ids.title);
    const periodFrom = $(ids.periodFrom);
    const periodTo = $(ids.periodTo);
    const messageTemplate = $(ids.messageTemplate);

    title?.addEventListener("input", (event) => syncComposerInput("title", event.target.value));
    periodFrom?.addEventListener("input", (event) => syncComposerInput("periodFrom", event.target.value));
    periodTo?.addEventListener("input", (event) => syncComposerInput("periodTo", event.target.value));
    messageTemplate?.addEventListener("input", (event) => syncComposerInput("messageTemplate", event.target.value));
  });
}

function bindEvents() {
  document.querySelectorAll("[data-mobile-view]").forEach((button) => {
    button.addEventListener("click", () => setMobileView(button.dataset.mobileView));
  });

  document.querySelectorAll('[data-action="reload-users"]').forEach((button) => {
    button.addEventListener("click", () => loadUsers({ reset: true }));
  });
  document.querySelectorAll('[data-action="load-more-users"]').forEach((button) => {
    button.addEventListener("click", () => loadUsers({ reset: false }));
  });
  document.querySelectorAll('[data-action="clear-selection"]').forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedUsers.clear();
      renderUI();
    });
  });
  document.querySelectorAll('[data-action="create-payout"]').forEach((button) => {
    button.addEventListener("click", createPayout);
  });
  document.querySelectorAll('[data-action="attach-selected"]').forEach((button) => {
    button.addEventListener("click", attachSelected);
  });
  document.querySelectorAll('[data-action="send-payout"]').forEach((button) => {
    button.addEventListener("click", sendPayout);
  });
  document.querySelectorAll('[data-action="export-csv"]').forEach((button) => {
    button.addEventListener("click", exportCsv);
  });
  document.querySelectorAll('[data-action="mark-paid"]').forEach((button) => {
    button.addEventListener("click", () => markPaid(Number(button.dataset.recipientId)));
  });

  $("close-payment-modal")?.addEventListener("click", closePaymentModal);
  $("close-payment-modal-secondary")?.addEventListener("click", closePaymentModal);
  $("copy-payment-details")?.addEventListener("click", copyPaymentDetails);
  $("payment-modal")?.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      closePaymentModal();
    }
  });
  $("error-banner")?.addEventListener("click", clearError);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.modalPaymentDetails) closePaymentModal();
  });

  const searchBindings = [
    ["desktop-search", "mobile-search"],
    ["desktop-has-payment", "mobile-has-payment"],
    ["desktop-is-active", "mobile-is-active"],
  ];

  searchBindings[0].forEach((id) => {
    $(id)?.addEventListener("input", (event) => {
      state.usersSearch = event.target.value;
      syncFilterInputs();
      if (state.searchTimer) clearTimeout(state.searchTimer);
      state.searchTimer = window.setTimeout(() => loadUsers({ reset: true }), SEARCH_DEBOUNCE_MS);
    });
  });
  searchBindings[1].forEach((id) => {
    $(id)?.addEventListener("change", (event) => {
      state.usersHasPayment = event.target.value;
      syncFilterInputs();
      loadUsers({ reset: true });
    });
  });
  searchBindings[2].forEach((id) => {
    $(id)?.addEventListener("change", (event) => {
      state.usersIsActive = event.target.value;
      syncFilterInputs();
      loadUsers({ reset: true });
    });
  });

  bindComposerInputs();
}

function syncFilterInputs() {
  ["desktop-search", "mobile-search"].forEach((id) => {
    const input = $(id);
    if (input) input.value = state.usersSearch;
  });
  ["desktop-has-payment", "mobile-has-payment"].forEach((id) => {
    const select = $(id);
    if (select) select.value = state.usersHasPayment;
  });
  ["desktop-is-active", "mobile-is-active"].forEach((id) => {
    const select = $(id);
    if (select) select.value = state.usersIsActive;
  });
}

function syncComposerFields() {
  getAllComposerElementPairs().forEach((fields) => {
    if (fields.title) fields.title.value = state.composer.title;
    if (fields.periodFrom) fields.periodFrom.value = state.composer.periodFrom;
    if (fields.periodTo) fields.periodTo.value = state.composer.periodTo;
    if (fields.messageTemplate) fields.messageTemplate.value = state.composer.messageTemplate;
    if (fields.validation) {
      fields.validation.textContent = "";
      fields.validation.classList.add("hidden");
    }
  });
  renderPreview();
}

async function bootstrap() {
  setThemeVariables();
  const webApp = telegramWebApp();
  if (webApp) {
    webApp.ready();
    webApp.expand();
  }

  state.composer.messageTemplate =
    "Всем привет!\n" +
    "Выплачиваем ЗАРПЛАТУ за работу в период с {period_from} по {period_to}.\n\n" +
    "Для получения выплаты проверьте или заполните платежные данные.\n\n" +
    "Если перевод на ваши данные:\n" +
    "укажите фамилию и имя, номер телефона для перевода / СБП, банк.\n\n" +
    "Если перевод на чужие данные:\n" +
    "укажите ваши фамилию и имя, имя владельца, номер телефона владельца, банк.\n\n" +
    "После сохранения бот должен ответить: «Ваше сообщение сохранено».";

  syncFilterInputs();
  syncComposerFields();
  renderAuthState();
  bindEvents();
  setMobileView(state.activeMobileView);
  renderUI();

  if (!canUseApi()) {
    return;
  }

  await Promise.all([loadUsers({ reset: true }), loadPayouts()]);
}

bootstrap();
