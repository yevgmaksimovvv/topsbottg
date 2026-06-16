import { state, canUseApi } from "./store.js";
import { emptyStateMarkup, escapeHtml, initials } from "./utils.js";
import { renderSelectedCount } from "./render-common.js";

import { usersEmptyMessage } from "./render-common.js";

function renderSelectionBarInto(rootId) {
  const root = document.getElementById(rootId);
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

export function renderSelectionBars() {
  renderSelectionBarInto("desktop-selection-bar");
  renderSelectionBarInto("mobile-selection-bar");
}

export function renderUsersInto(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return;
  const empty = usersEmptyMessage();
  if (empty) {
    root.innerHTML = emptyStateMarkup("Пользователи", empty);
    return;
  }
  root.innerHTML = state.users
    .map((user) => {
      return `
        <article class="user-card">
          <div class="avatar" aria-hidden="true">${escapeHtml(initials(user.full_name))}</div>
          <div class="user-main">
            <div class="user-line">
              <strong>${escapeHtml(user.full_name)}</strong>
            </div>
            <div class="meta">Telegram ID: ${escapeHtml(user.telegram_user_id || user.telegram_id)}</div>
          </div>
          <div class="user-actions">
            <label class="checkbox-cell" aria-label="Выбрать пользователя">
              <input type="checkbox" data-user-id="${user.id}" ${state.selectedUsers.has(user.id) ? "checked" : ""} ${!canUseApi() ? "disabled" : ""} />
            </label>
          </div>
        </article>`;
    })
    .join("");
}

export function renderUsers() {
  renderUsersInto("desktop-users");
  renderUsersInto("mobile-users");
  renderSelectedCount();
  renderSelectionBars();
}
