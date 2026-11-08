import { state, canUseApi, $ } from "./store.js";
import { escapeHtml, plural } from "./utils.js";
function userMarkup(user, locked) {
  const id = Number(user.id),
    selected = state.selectedUsers.has(id);
  return `<label class="user-row${selected ? " selected" : ""}${locked ? " is-locked" : ""}" data-user-row="${id}"><span class="user-name">${escapeHtml(user.full_name || "")}</span><input type="checkbox" data-user-id="${id}" aria-label="Выбрать ${escapeHtml(user.full_name || "")}" ${selected ? "checked" : ""} ${locked ? "disabled" : ""}></label>`;
}
export function renderUsersInto() {
  const root = $("users-list");
  if (!root) return;
  const locked =
      !canUseApi() ||
      !state.selectedPayoutDetail ||
      state.selectedPayoutDetail.payout?.status !== "draft",
    note = $("users-lock-note");
  if (note) {
    note.textContent = locked
      ? state.selectedPayoutDetail
        ? "Список можно менять только у выплаты в статусе «Черновик»."
        : "Сначала выберите черновик выплаты в истории."
      : "";
    note.classList.toggle("hidden", !locked);
  }
  const list = state.users,
    ids = list.map((u) => String(Number(u.id))),
    old = (root.dataset.userIds || "").split(",").filter(Boolean),
    append =
      old.length > 0 &&
      root.dataset.locked === String(locked) &&
      ids.length > old.length &&
      old.every((id, i) => ids[i] === id);
  if (append)
    root.insertAdjacentHTML(
      "beforeend",
      list
        .slice(old.length)
        .map((u) => userMarkup(u, locked))
        .join(""),
    );
  else if (
    ids.join(",") !== old.join(",") ||
    root.dataset.locked !== String(locked)
  ) {
    root.innerHTML = list.map((u) => userMarkup(u, locked)).join("");
  }
  for (const user of list) {
    const id = Number(user.id);
    const row = root.querySelector(`[data-user-row="${id}"]`);
    const checkbox = row?.querySelector("input[data-user-id]");
    const selected = state.selectedUsers.has(id);
    if (checkbox) checkbox.checked = selected;
    row?.classList.toggle("selected", selected);
    const name = row?.querySelector(".user-name");
    if (name && name.textContent !== (user.full_name || ""))
      name.textContent = user.full_name || "";
    if (checkbox) {
      checkbox.disabled = locked;
      checkbox.setAttribute(
        "aria-label",
        `Выбрать ${user.full_name || ""}`,
      );
    }
  }
  root.dataset.userIds = ids.join(",");
  root.dataset.locked = String(locked);
  root.dataset.payoutId = String(state.selectedPayoutId ?? "");
  root.dataset.selectedUsers = [...state.selectedUsers].sort((a, b) => a - b).join(",");
  const count = $("selected-count");
  if (count)
    count.textContent = `${state.selectedUsers.size} ${plural(state.selectedUsers.size, "получатель", "получателя", "получателей")}`;
  const send = document.querySelector('[data-action="send-payout"]');
  if (send)
    send.disabled =
      locked ||
      state.selectedUsers.size === 0 ||
      state.loading.sendPayout;
  $("selection-bar")?.classList.toggle(
    "hidden",
    state.activeMobileView !== "users" ||
      locked ||
      state.selectedUsers.size === 0,
  );
  document
    .querySelector('[data-action="load-more-users"]')
    ?.classList.toggle("hidden", !state.usersHasMore);
  const status = $("users-state");
  if (status)
    status.textContent =
      state.loading.users && !list.length
        ? "Загрузка пользователей…"
        : state.usersLoadError
          ? `Не удалось загрузить пользователей: ${state.usersLoadError}. Измените запрос или попробуйте ещё раз.`
          : !list.length
            ? canUseApi()
              ? "Пользователи не найдены."
              : "Откройте приложение через Telegram, чтобы загрузить пользователей."
            : "";
  const more = $("users-more-state");
  if (more)
    more.textContent =
      state.loading.users && list.length
        ? "Загружаем…"
        : state.usersLoadError && list.length
          ? "Не удалось загрузить страницу. Загрузите ещё раз."
          : canUseApi() && !state.usersHasMore && list.length
            ? "Больше результатов нет."
            : "";
}
export function renderUsers() {
  renderUsersInto();
}
