import { state, canUseApi } from "./store.js";
import { emptyStateMarkup, escapeHtml, loadingStateMarkup } from "./utils.js";
import { renderSelectedCount, usersEmptyMessage } from "./render-common.js";

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
          <label class="user-row${locked ? " is-locked" : ""}" aria-label="Выбрать пользователя">
            <strong class="user-name">${escapeHtml(user.full_name)}</strong>
            <span class="user-checkbox-wrap">
              <input type="checkbox" class="user-checkbox" data-user-id="${user.id}" ${state.selectedUsers.has(user.id) ? "checked" : ""} ${locked ? "disabled" : ""} />
              <span class="user-checkbox-box" aria-hidden="true"></span>
            </span>
          </label>
        </article>`;
    })
    .join("");
}

export function renderUsers() {
  renderUsersInto("desktop-users");
  renderUsersInto("mobile-users");
  renderSelectedCount();
}
