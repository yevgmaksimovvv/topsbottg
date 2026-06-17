import { PAYOUT_STATUS_LABELS } from "./constants.js";
import { state, canUseApi } from "./store.js";
import { emptyStateMarkup, escapeHtml, loadingStateMarkup, statusLabel } from "./utils.js";
import { renderSelectedCount, usersEmptyMessage } from "./render-common.js";

function selectedPayoutText() {
  const payout = state.selectedPayoutDetail?.payout;
  if (!payout) return "";
  const status = statusLabel(PAYOUT_STATUS_LABELS, payout.status);
  return `Выплата #${payout.id} · ${status}`;
}

function renderUsersContextInto(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return;
  const payout = state.selectedPayoutDetail?.payout || null;
  if (!canUseApi()) {
    root.classList.add("hidden");
    root.innerHTML = "";
    return;
  }
  root.classList.remove("hidden");
  if (!payout) {
    root.innerHTML = `<div class="users-context"><span>Сначала выберите выплату в списке.</span></div>`;
    return;
  }
  if (payout.status !== "draft") {
    root.innerHTML = `
      <div class="users-context users-context-warning">
        <span>Выбрана выплата ${escapeHtml(selectedPayoutText())}. Пользователей можно менять только у черновика.</span>
      </div>`;
    return;
  }
  const hasSelection = state.selectedUsers.size > 0;
  root.innerHTML = `
    <div class="users-context">
      <div class="users-context-copy">
        <strong>${escapeHtml(selectedPayoutText())}</strong>
        <span>${hasSelection ? "Выбор привязан к этой выплате. Перед отправкой будут добавлены только недостающие получатели." : "Выбор привязан к этой выплате. Отметьте пользователей ниже."}</span>
      </div>
      ${hasSelection ? `<button type="button" data-action="send-payout">${state.loading.sendPayout ? '<span class="spinner"></span> Разосылаем…' : "Разослать"}</button>` : ""}
    </div>`;
}

export function renderSelectionBars() {
  const renderSelectionBarInto = (rootId) => {
    const root = document.getElementById(rootId);
    if (!root) return;
    if (!state.selectedUsers.size) {
      root.classList.add("hidden");
      root.innerHTML = "";
      return;
    }
    root.classList.remove("hidden");
    const payout = state.selectedPayoutDetail?.payout || null;
    const note = !payout
      ? "Сначала выберите выплату в списке."
      : payout.status !== "draft"
        ? "Для этой выплаты добавление закрыто."
        : "Выбор готов к отправке сверху.";
    root.innerHTML = `
      <div class="selection-bar-copy">
        <div class="selected-counter">
          <span>Выбрано</span>
          <strong>${state.selectedUsers.size}</strong>
        </div>
        <span>${note}</span>
      </div>`;
  };
  renderSelectionBarInto("desktop-selection-bar");
  renderSelectionBarInto("mobile-selection-bar");
}

export function renderUsersInto(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return;
  if (state.loading.users && !state.users.length) {
    root.innerHTML = loadingStateMarkup("Пользователи", "Загрузка…");
    return;
  }
  const empty = usersEmptyMessage();
  if (empty) {
    root.innerHTML = emptyStateMarkup("Пользователи", empty);
    return;
  }
  root.innerHTML = state.users
    .map((user) => {
      const locked = !canUseApi() || !state.selectedPayoutDetail || state.selectedPayoutDetail.payout.status !== "draft";
      return `
        <article class="user-card">
          <div class="user-main">
            <strong>${escapeHtml(user.full_name)}</strong>
          </div>
          <div class="user-actions">
            <label class="checkbox-cell" aria-label="Выбрать пользователя">
              <input type="checkbox" data-user-id="${user.id}" ${state.selectedUsers.has(user.id) ? "checked" : ""} ${locked ? "disabled" : ""} />
            </label>
          </div>
        </article>`;
    })
    .join("");
}

export function renderUsers() {
  renderUsersContextInto("desktop-users-context");
  renderUsersContextInto("mobile-users-context");
  renderUsersInto("desktop-users");
  renderUsersInto("mobile-users");
  renderSelectedCount();
  renderSelectionBars();
}
