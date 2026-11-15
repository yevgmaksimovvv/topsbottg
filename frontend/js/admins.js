import { api } from "./api.js";
import { $, canUseApi, state } from "./store.js";
import { telegramWebApp } from "./telegram.js";
import { escapeHtml } from "./utils.js";

const dialog = () => $("admins-dialog");
let dialogRequestId = 0;
let searchRequestId = 0;
let opener = null;
let telegramBack = null;
let busy = false;
let members = [];
let invitations = [];
let candidate = null;
let pendingInvite = false;
let pendingRevoke = null;
let loadingMembers = false;
let loadingSearch = false;
let memberError = "";
let searchError = "";

const nameFor = (user) => user?.username
  ? `@${user.username.replace(/^@/, "")}`
  : (user?.display_name && user.display_name !== "Без ФИО"
      ? user.display_name
      : "Имя не указано");

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
  if (!candidate) {
    target.replaceChildren();
    return;
  }
  const display = candidate.full_name && candidate.full_name !== "Без ФИО"
    ? candidate.full_name
    : candidate.display_name || "Имя не указано";
  const group = candidate.study_group ? ` · ${candidate.study_group}` : "";
  target.innerHTML = `<article class="admins-candidate-card">
    <div class="admins-member-identity">
      <strong>${escapeHtml(nameFor(candidate))}</strong>
      <span>${escapeHtml(display + group)}</span>
    </div>
    ${candidate.role
      ? `<p class="admins-empty">${candidate.role === "owner"
          ? "Уже владелец" : "Уже администратор"}</p>`
      : pendingInvite
      ? `<p class="admins-revoke-warning">Отправить приглашение этому пользователю?</p>
         <div class="admins-candidate-actions">
           <button type="button" class="button button-primary"
             data-admin-action="confirm-invite" ${busy ? "disabled" : ""}>Отправить</button>
           <button type="button" class="button button-quiet"
             data-admin-action="cancel-invite-confirm" ${busy ? "disabled" : ""}>Отмена</button>
         </div>`
      : `<button type="button" class="button button-primary"
           data-admin-action="invite" ${busy ? "disabled" : ""}>Пригласить</button>`}
  </article>`;
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

async function searchCandidate() {
  if (!dialog()?.open || state.adminRole !== "owner" || busy) return;
  const value = $("admins-search-input")?.value.trim() || "";
  const id = ++searchRequestId;
  candidate = null;
  pendingInvite = false;
  searchError = "";
  if (!value) {
    searchError = "Введите username или Telegram ID.";
    renderCandidate();
    return;
  }
  loadingSearch = true;
  renderCandidate();
  try {
    const params = /^\d+$/.test(value)
      ? new URLSearchParams({ telegram_id: value })
      : new URLSearchParams({ username: value });
    const results = await api(`/admin/admin-candidates?${params}`);
    if (searchRequestId !== id || !dialog()?.open) return;
    candidate = Array.isArray(results) ? results[0] || null : null;
    if (!candidate) searchError = "Пользователь не найден. Сначала он должен открыть бота через /start.";
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
  } else if (action === "invite" && candidate) {
    pendingInvite = true;
    renderCandidate();
  } else if (action === "cancel-invite-confirm") {
    pendingInvite = false;
    renderCandidate();
  } else if (action === "confirm-invite" && pendingInvite && candidate) {
    const selected = candidate;
    void mutateAdmin(() => api("/admin/admin-invitations", {
      method: "POST",
      body: JSON.stringify({
        target_telegram_id: selected.telegram_id,
        ...(selected.username ? { expected_username: selected.username } : {}),
      }),
    }));
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
  });
  $("admins-close")?.addEventListener("click", closeAdminDialog);
  $("admins-refresh")?.addEventListener("click", () => void refreshDirectory());
  $("admins-search-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    void searchCandidate();
  });
  $("admins-search-input")?.addEventListener("input", () => {
    ++searchRequestId;
    candidate = null;
    pendingInvite = false;
    searchError = "";
    loadingSearch = false;
    renderCandidate();
  });
  dialog()?.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-admin-action]");
    if (button && !button.disabled) handleAction(button);
  });
  dialog()?.addEventListener("cancel", releaseTelegramBackButton);
  dialog()?.addEventListener("close", () => {
    ++dialogRequestId;
    ++searchRequestId;
    releaseTelegramBackButton();
    pendingInvite = false;
    pendingRevoke = null;
    candidate = null;
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
