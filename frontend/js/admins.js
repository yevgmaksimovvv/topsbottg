import { api } from "./api.js";
import { SEARCH_DEBOUNCE_MS } from "./constants.js";
import { $, canUseApi, state } from "./store.js";
import { telegramWebApp } from "./telegram.js";
import { escapeHtml } from "./utils.js";

const dialog = () => $("admins-dialog");
let dialogRequestId = 0;
let searchRequestId = 0;
let searchTimer = null;
let opener = null;
let telegramBack = null;
let busy = false;
let members = [];
let invitations = [];
let candidate = null;
let candidates = [];
let hasMoreCandidates = false;
let pendingInvite = false;
let pendingRevoke = null;
let loadingMembers = false;
let loadingSearch = false;
let memberError = "";
let searchError = "";

const nameFor = (user) => user?.full_name || (user?.telegram_id ? `ID ${user.telegram_id}` : "Имя не указано");

function renderMembers() {
  const status = $("admins-status");
  const list = $("admins-members");
  if (!list || !status) return;
  status.textContent = memberError || (loadingMembers ? "Загружаем список…" : "");
  if (loadingMembers && !members.length) {
    list.replaceChildren();
    return;
  }
  if (!members.length) {
    list.innerHTML = '<p class="admins-empty">Администраторы не найдены.</p>';
    return;
  }
  list.innerHTML = members.map((member) => {
    const id = Number(member.telegram_id);
    const role = member.role === "owner" ? "Владелец" : "Администратор";
    const confirm = pendingRevoke === id;
    return `<article class="admins-member">
      <div class="admins-member-identity">
        <strong>${escapeHtml(nameFor(member))}</strong>
        <span>${role}</span>
      </div>
      ${member.role === "admin"
        ? `<div class="admins-member-actions">
          ${confirm
            ? `<span class="admins-revoke-warning">Отозвать доступ?</span>
               <button type="button" class="admins-text-button admins-danger"
                 data-admin-action="confirm-revoke" data-admin-id="${id}" ${busy ? "disabled" : ""}>Да, отозвать</button>
               <button type="button" class="admins-text-button"
                 data-admin-action="cancel-revoke">Отмена</button>`
            : `<button type="button" class="admins-text-button admins-danger"
                 data-admin-action="revoke" data-admin-id="${id}" ${busy ? "disabled" : ""}>Отозвать</button>`
          }
        </div>`
        : ""}
    </article>`;
  }).join("");
}

function renderInvitations() {
  const list = $("admins-pending");
  if (!list) return;
  const pending = invitations.filter((item) => item.status === "pending");
  list.innerHTML = pending.length
    ? pending.map((item) => `<article class="admins-member">
        <div class="admins-member-identity">
          <strong>${escapeHtml(nameFor(item))}</strong>
          <span>${item.delivery_state === "delivered" || item.delivered_at
            ? "Ожидает ответа"
            : item.delivery_state === "unknown" ? "Результат отправки неизвестен"
            : item.delivery_state === "sending" ? "Отправляется"
            : "В очереди на отправку"}</span>
        </div>
        <button type="button" class="admins-text-button"
          data-admin-action="cancel-invite" data-invite-id="${Number(item.id)}"
          ${busy ? "disabled" : ""}>Отменить</button>
      </article>`).join("")
    : '<p class="admins-empty">Нет ожидающих приглашений.</p>';
}

function renderCandidate() {
  const target = $("admins-candidate");
  const status = $("admins-search-error");
  if (!target || !status) return;
  status.textContent = searchError || (loadingSearch ? "Ищем пользователя…" : "");
  target.innerHTML = candidates.map((user) => {
    const id = Number(user.telegram_id);
    const selected = candidate?.telegram_id === id;
    const alreadyInvited = invitations.some((item) =>
      item.status === "pending" && Number(item.target_telegram_id) === id);
    const group = user.study_group || "Группа не указана";
    return `<article class="admins-candidate-card">
      <div class="admins-member-identity">
        <strong>${escapeHtml(nameFor(user))}</strong>
        <span>${escapeHtml(group)} · ID ${id}</span>
      </div>
      ${user.role
        ? `<p class="admins-empty">${user.role === "owner" ? "Уже владелец" : "Уже администратор"}</p>`
        : alreadyInvited
          ? '<p class="admins-empty">Приглашение уже отправлено</p>'
          : selected && pendingInvite
            ? `<p class="admins-revoke-warning">Отправить приглашение этому пользователю?</p>
               <div class="admins-candidate-actions">
                 <button type="button" class="button button-primary"
                   data-admin-action="confirm-invite" ${busy ? "disabled" : ""}>Отправить</button>
                 <button type="button" class="button button-quiet"
                   data-admin-action="cancel-invite-confirm" ${busy ? "disabled" : ""}>Отмена</button>
               </div>`
            : `<button type="button" class="button button-primary"
                 data-admin-action="invite" data-admin-id="${id}" ${busy ? "disabled" : ""}>Пригласить</button>`}
    </article>`;
  }).join("") + (hasMoreCandidates
    ? `<button type="button" class="button button-quiet admins-load-more"
         data-admin-action="more-candidates" ${loadingSearch || busy ? "disabled" : ""}>Показать ещё</button>`
    : "");
}

async function loadStudyGroups() {
  const select = $("admins-search-group");
  if (!select) return;
  try {
    const groups = await api("/admin/study-groups");
    if (!dialog()?.open) return;
    const previous = select.value;
    select.innerHTML = '<option value="">Все группы</option>' +
      groups.map((group) => `<option value="${Number(group.id)}">${escapeHtml(group.code)}</option>`).join("");
    if (Array.from(select.options).some((option) => option.value === previous))
      select.value = previous;
  } catch {
    // Search without the group filter must remain available.
  }
}

async function refreshDirectory() {
  if (!dialog()?.open || state.adminRole !== "owner") return;
  const requestId = ++dialogRequestId;
  loadingMembers = true;
  memberError = "";
  renderMembers();
  try {
    const [directory, pending] = await Promise.all([
      api("/admin/admin-directory"),
      api("/admin/admin-invitations"),
    ]);
    if (requestId !== dialogRequestId || !dialog()?.open) return;
    members = Array.isArray(directory) ? directory : [];
    invitations = Array.isArray(pending) ? pending : [];
  } catch (error) {
    if (requestId !== dialogRequestId || !dialog()?.open) return;
    memberError = error?.message || "Не удалось обновить список.";
  } finally {
    if (requestId === dialogRequestId && dialog()?.open) {
      loadingMembers = false;
      renderMembers();
      renderInvitations();
    }
  }
}

function releaseTelegramBackButton() {
  const backButton = telegramBack;
  telegramBack = null;
  backButton?.offClick?.(closeAdminDialog);
  backButton?.hide?.();
}

function closeAdminDialog() {
  releaseTelegramBackButton();
  if (dialog()?.open) dialog().close();
}

function cancelScheduledSearch() {
  if (searchTimer !== null) window.clearTimeout(searchTimer);
  searchTimer = null;
}

function scheduleCandidateSearch(immediate = false) {
  cancelScheduledSearch();
  ++searchRequestId;
  candidates = [];
  candidate = null;
  pendingInvite = false;
  hasMoreCandidates = false;
  searchError = "";
  loadingSearch = false;
  renderCandidate();

  const value = $("admins-search-input")?.value.trim() || "";
  const groupId = $("admins-search-group")?.value || "";
  if ((!value && !groupId) || (value && !/^\\d+$/.test(value) && value.length < 2)) return;
  if (immediate) {
    void searchCandidate();
  } else {
    searchTimer = window.setTimeout(() => {
      searchTimer = null;
      void searchCandidate();
    }, SEARCH_DEBOUNCE_MS);
  }
}

async function searchCandidate(append = false) {
  if (!dialog()?.open || state.adminRole !== "owner" || busy || loadingSearch) return;
  const value = $("admins-search-input")?.value.trim() || "";
  const groupId = $("admins-search-group")?.value || "";
  if (!value && !groupId) return;
  const id = ++searchRequestId;
  searchError = "";
  loadingSearch = true;
  renderCandidate();
  try {
    const params = new URLSearchParams({ offset: String(candidates.length) });
    if (value) params.set("search", value);
    if (groupId) params.set("study_group_id", groupId);
    const result = await api(`/admin/admin-candidates?${params}`);
    if (searchRequestId !== id || !dialog()?.open) return;
    const found = Array.isArray(result?.items) ? result.items : [];
    candidates = append ? [...candidates, ...found] : found;
    hasMoreCandidates = result?.has_more === true;
    if (!candidates.length) searchError = "Зарегистрированные пользователи не найдены.";
  } catch (error) {
    if (searchRequestId !== id || !dialog()?.open) return;
    searchError = error?.message || "Не удалось найти пользователя.";
  } finally {
    if (searchRequestId === id && dialog()?.open) {
      loadingSearch = false;
      renderCandidate();
    }
  }
}

async function mutateAdmin(action) {
  if (busy || state.adminRole !== "owner" || !dialog()?.open) return;
  busy = true;
  searchError = "";
  memberError = "";
  renderMembers();
  renderInvitations();
  renderCandidate();
  try {
    await action();
    pendingRevoke = null;
    pendingInvite = false;
    candidate = null;
    renderCandidate();
    await refreshDirectory();
  } catch (error) {
    memberError = error?.message || "Не удалось выполнить действие.";
    renderMembers();
    if (candidate) {
      searchError = memberError;
      renderCandidate();
    }
  } finally {
    busy = false;
    renderMembers();
    renderInvitations();
    renderCandidate();
  }
}

function handleAction(button) {
  const action = button.dataset.adminAction;
  if (action === "revoke") {
    pendingRevoke = Number(button.dataset.adminId);
    renderMembers();
  } else if (action === "cancel-revoke") {
    pendingRevoke = null;
    renderMembers();
  } else if (action === "confirm-revoke") {
    const id = Number(button.dataset.adminId);
    if (pendingRevoke === id && members.some((member) =>
      member.role === "admin" && Number(member.telegram_id) === id)) {
      void mutateAdmin(() => api(`/admin/admin-grants/${id}`, { method: "DELETE" }));
    }
  } else if (action === "invite") {
    const id = Number(button.dataset.adminId);
    const found = candidates.find((item) => Number(item.telegram_id) === id && !item.role);
    if (!found || invitations.some((item) => item.status === "pending" && Number(item.target_telegram_id) === id)) return;
    candidate = found;
    pendingInvite = true;
    renderCandidate();
  } else if (action === "cancel-invite-confirm") {
    pendingInvite = false;
    renderCandidate();
  } else if (action === "confirm-invite" && pendingInvite && candidate) {
    const selected = candidate;
    void mutateAdmin(() => api("/admin/admin-invitations", {
      method: "POST",
      body: JSON.stringify({ target_telegram_id: selected.telegram_id }),
    }));
  } else if (action === "more-candidates") {
    void searchCandidate(true);
  } else if (action === "cancel-invite") {
    const id = Number(button.dataset.inviteId);
    if (invitations.some((invitation) => invitation.status === "pending" && Number(invitation.id) === id))
      void mutateAdmin(() => api(`/admin/admin-invitations/${id}/cancel`, { method: "POST" }));
  }
}

export function bindAdminControls() {
  $("admins-open")?.addEventListener("click", () => {
    if (state.adminRole !== "owner" || !canUseApi() || dialog()?.open) return;
    opener = document.activeElement;
    const overlay = dialog();
    overlay.showModal();
    $("admins-close")?.focus();
    telegramBack = telegramWebApp()?.BackButton;
    telegramBack?.onClick?.(closeAdminDialog);
    telegramBack?.show?.();
    void refreshDirectory();
    void loadStudyGroups();
    scheduleCandidateSearch(true);
  });
  $("admins-close")?.addEventListener("click", closeAdminDialog);
  $("admins-refresh")?.addEventListener("click", () => void refreshDirectory());
  $("admins-search-input")?.addEventListener("input", () => scheduleCandidateSearch());
  $("admins-search-group")?.addEventListener("change", () => scheduleCandidateSearch(true));
  dialog()?.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-admin-action]");
    if (button && !button.disabled) handleAction(button);
  });
  dialog()?.addEventListener("cancel", releaseTelegramBackButton);
  dialog()?.addEventListener("close", () => {
    cancelScheduledSearch();
    ++dialogRequestId;
    ++searchRequestId;
    releaseTelegramBackButton();
    pendingInvite = false;
    pendingRevoke = null;
    candidate = null;
    candidates = [];
    hasMoreCandidates = false;
    searchError = "";
    loadingSearch = false;
    if (opener?.isConnected && !opener.classList.contains("hidden")) opener.focus();
    opener = null;
  });
}

export async function initAdminControls() {
  if (!canUseApi()) return;
  try {
    const me = await api("/admin/me");
    state.adminRole = me.role === "owner" ? "owner" : "admin";
  } catch {
    state.adminRole = null;
  }
  $("admins-open")?.classList.toggle("hidden", state.adminRole !== "owner");
}
