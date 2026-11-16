import { SEARCH_DEBOUNCE_MS } from "./constants.js";
import { state } from "./store.js";
import { escapeHtml } from "./utils.js";

const el = (id) => document.getElementById(id);
const html = (value) => escapeHtml(String(value ?? ""));
const statuses = {
  queued: "В очереди",
  sending: "Отправляется",
  sent: "Отправлено",
  failed: "Ошибка доставки",
  partially_failed: "Частично доставлено",
};
const identityKey = () => window.crypto?.randomUUID?.() ||
  Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
const campaign = {
  client: null,
  onAuthError: null,
  screen: null,
  userIds: new Set(),
  groupIds: new Set(),
  names: new Map(),
  users: [],
  search: "",
  filterGroup: "",
  offset: 0,
  hasMore: false,
  userBusy: false,
  userGeneration: 0,
  userController: null,
  searchTimer: null,
  text: "",
  version: 0,
  preview: null,
  previewBusy: false,
  previewController: null,
  sending: false,
  submission: null,
  history: [],
  cursor: null,
  historyBusy: false,
  historyGeneration: 0,
  historyController: null,
  detail: null,
  detailGeneration: 0,
  detailController: null,
  retryBusy: false,
  eventTimer: null,
};

const message = (id, value) => {
  const node = el(id);
  if (node) node.textContent = value || "";
};
const setHidden = (id, hidden) => {
  const node = el(id);
  if (node) node.hidden = hidden;
};
const failure = (error, target) => {
  if (error?.name === "AbortError") return true;
  if (error?.status === 401 || error?.status === 403) {
    campaign.onAuthError?.(error);
    return true;
  }
  message(target, error?.message || "Сервер временно недоступен. Повторите запрос.");
  return false;
};
const criteria = () => ({
  user_ids: [...campaign.userIds].sort((a, b) => a - b),
  study_group_ids: [...campaign.groupIds].sort((a, b) => a - b),
});

function abortRequests() {
  campaign.userGeneration++;
  campaign.historyGeneration++;
  campaign.detailGeneration++;
  campaign.userController?.abort();
  campaign.historyController?.abort();
  campaign.detailController?.abort();
  campaign.previewController?.abort();
  campaign.userController = null;
  campaign.historyController = null;
  campaign.detailController = null;
  campaign.previewController = null;
  clearTimeout(campaign.searchTimer);
  clearTimeout(campaign.eventTimer);
  campaign.userBusy = false;
  campaign.historyBusy = false;
  campaign.previewBusy = false;
}

function invalidatePreview() {
  campaign.version++;
  campaign.previewController?.abort();
  campaign.preview = null;
  campaign.submission = null;
  setHidden("campaign-preview", true);
  message("campaign-compose-error", "");
  message("campaign-send-status", "");
  updateSubmit();
}

function updateSubmit() {
  const preview = el("campaign-preview-button");
  const confirm = el("campaign-confirm-button");
  if (preview) preview.disabled = campaign.previewBusy || campaign.sending;
  if (confirm) confirm.disabled = campaign.sending || !campaign.preview ||
    campaign.preview.version !== campaign.version || campaign.preview.recipient_count === 0;
  const count = campaign.userIds.size;
  const groups = campaign.groupIds.size;
  message("campaign-selection", "Выбрано вручную: " + count + " · Групп: " + groups +
    ". Итоговый уникальный состав определяет сервер.");
}

function renderGroups() {
  const node = el("campaign-group-options");
  if (!node) return;
  node.innerHTML = state.studyGroups.map((group) =>
    '<label class="campaign-group"><input type="checkbox" data-campaign-group="' + Number(group.id) +
    '"' + (campaign.groupIds.has(Number(group.id)) ? " checked" : "") +
    '> ' + html(group.code || group.display_code) + "</label>").join("");
  const filter = el("campaign-user-filter");
  filter.replaceChildren(new Option("Все группы", ""));
  for (const group of state.studyGroups)
    filter.add(new Option(group.code || group.display_code, String(group.id)));
  filter.value = campaign.filterGroup;
}

function renderUsers() {
  const node = el("campaign-users");
  if (!node) return;
  node.innerHTML = campaign.users.map((user) => {
    const id = Number(user.id);
    return '<label class="campaign-user"><input type="checkbox" data-campaign-user="' + id + '"' +
      (campaign.userIds.has(id) ? " checked" : "") +
      '><span><strong>' + html(user.full_name) + "</strong>" +
      '<small>' + html(user.study_group?.code || "Без группы") + "</small></span></label>";
  }).join("");
  if (!campaign.users.length && !campaign.userBusy)
    node.innerHTML = '<p class="campaign-empty">Пользователи не найдены.</p>';
  setHidden("campaign-more-users", !campaign.hasMore);
  el("campaign-more-users").disabled = campaign.userBusy;
  updateSubmit();
}

async function loadUsers(reset = false) {
  if (campaign.screen !== "compose" || !campaign.client ||
      (campaign.userBusy && !reset)) return;
  const token = ++campaign.userGeneration;
  campaign.userController?.abort();
  const controller = new AbortController();
  campaign.userController = controller;
  if (reset) {
    campaign.users = [];
    campaign.offset = 0;
    campaign.hasMore = false;
  }
  campaign.userBusy = true;
  message("campaign-user-status", "Поиск пользователей…");
  renderUsers();
  try {
    const result = await campaign.client.users({
      search: campaign.search, study_group_id: campaign.filterGroup,
      offset: campaign.offset, limit: 50, signal: controller.signal,
    });
    if (token !== campaign.userGeneration || campaign.screen !== "compose" ||
        controller.signal.aborted) return;
    const ids = new Set(campaign.users.map((user) => Number(user.id)));
    for (const user of result.items || []) {
      const id = Number(user.id);
      if (!ids.has(id)) {
        campaign.users.push(user);
        ids.add(id);
      }
      campaign.names.set(id, user.full_name);
    }
    campaign.offset = Number(result.offset || 0) + (result.items || []).length;
    campaign.hasMore = Boolean(result.has_more);
    message("campaign-user-status", "");
  } catch (error) {
    if (token === campaign.userGeneration) failure(error, "campaign-user-status");
  } finally {
    if (token === campaign.userGeneration) {
      campaign.userBusy = false;
      renderUsers();
    }
  }
}

function showScreen(type) {
  campaign.screen = type;
  for (const id of [
    "inbox-list-screen", "inbox-new-screen", "inbox-history-screen",
    "inbox-campaign-compose-screen", "inbox-campaign-history-screen",
  ]) setHidden(id, id !== (type === "compose"
    ? "inbox-campaign-compose-screen" : "inbox-campaign-history-screen"));
  setHidden("inbox-new", true);
  setHidden("inbox-back", false);
  el("inbox-title").textContent = type === "compose" ? "Новое сообщение" : "История рассылок";
}

export function campaignsActive() {
  return campaign.screen !== null;
}

export function leaveCampaignScreens() {
  if (!campaign.screen) return;
  abortRequests();
  campaign.screen = null;
  setHidden("inbox-campaign-compose-screen", true);
  setHidden("inbox-campaign-history-screen", true);
  campaign.detail = null;
  campaign.retryBusy = false;
}

export function openCampaignCompose(client, onAuthError) {
  if (!client || typeof client.campaignPreview !== "function") return;
  leaveCampaignScreens();
  campaign.client = client;
  campaign.onAuthError = onAuthError;
  showScreen("compose");
  el("campaign-text").value = campaign.text;
  el("campaign-user-search").value = campaign.search;
  renderGroups();
  renderUsers();
  setHidden("campaign-preview", !campaign.preview);
  if (campaign.preview) renderPreview();
  updateSubmit();
  void loadUsers(true);
}

export function openCampaignHistory(client, onAuthError) {
  if (!client || typeof client.campaignList !== "function") return;
  leaveCampaignScreens();
  campaign.client = client;
  campaign.onAuthError = onAuthError;
  showScreen("history");
  campaign.detail = null;
  setHidden("campaign-detail", true);
  void loadHistory(true);
}

function renderPreview() {
  const preview = campaign.preview;
  setHidden("campaign-preview", !preview);
  if (!preview) return;
  message("campaign-preview-count", "Уникальных получателей: " + preview.recipient_count);
  message("campaign-preview-text", campaign.text);
  el("campaign-preview-users").innerHTML = preview.recipients.map((user) =>
    '<div class="campaign-preview-user">' + html(user.full_name) +
    '<small>' + html(user.study_group_name || "Без группы") + "</small></div>").join("");
  updateSubmit();
}

async function previewCampaign() {
  if (!campaign.client || campaign.previewBusy || campaign.sending) return;
  const text = campaign.text;
  const selected = criteria();
  if (!text.trim() || text.length > 4096) {
    message("campaign-compose-error", "Введите текст от 1 до 4096 символов.");
    return;
  }
  if (!selected.user_ids.length && !selected.study_group_ids.length) {
    message("campaign-compose-error", "Выберите пользователя или группу.");
    return;
  }
  const generation = campaign.version;
  const controller = new AbortController();
  campaign.previewController?.abort();
  campaign.previewController = controller;
  campaign.previewBusy = true;
  message("campaign-compose-error", "");
  message("campaign-send-status", "Проверяем адресатов…");
  updateSubmit();
  try {
    const result = await campaign.client.campaignPreview(selected, { signal: controller.signal });
    if (campaign.screen !== "compose" || generation !== campaign.version ||
        controller.signal.aborted) return;
    campaign.preview = { ...result, version: generation };
    if (!result.recipient_count) {
      message("campaign-compose-error", "По выбранным критериям нет зарегистрированных адресатов.");
    }
    message("campaign-send-status", "");
    renderPreview();
  } catch (error) {
    if (generation === campaign.version) {
      campaign.preview = null;
      failure(error, "campaign-compose-error");
    }
  } finally {
    if (campaign.previewController === controller) campaign.previewController = null;
    campaign.previewBusy = false;
    updateSubmit();
  }
}

async function confirmCampaign() {
  if (campaign.sending || !campaign.preview ||
      campaign.preview.version !== campaign.version ||
      campaign.preview.recipient_count === 0) return;
  const selected = criteria();
  const text = campaign.text;
  const generation = campaign.version;
  if (!campaign.submission || campaign.submission.version !== generation)
    campaign.submission = {
      ...selected, text, version: generation, idempotency_key: identityKey(),
    };
  const submission = campaign.submission;
  campaign.sending = true;
  message("campaign-compose-error", "");
  message("campaign-send-status", "Создаём кампанию…");
  updateSubmit();
  try {
    const created = await campaign.client.campaignCreate(submission);
    if (campaign.screen !== "compose") return;
    if (generation !== campaign.version) return;
    campaign.userIds.clear();
    campaign.groupIds.clear();
    campaign.text = "";
    campaign.preview = null;
    campaign.submission = null;
    campaign.version++;
    message("campaign-send-status", "Кампания #" + created.id + " создана. Адресатов: " +
      created.recipient_count + ".");
    el("campaign-text").value = "";
    openCampaignHistory(campaign.client, campaign.onAuthError);
    message("campaign-history-status", "Кампания #" + created.id +
      " поставлена в очередь для " + created.recipient_count + " получателей.");
  } catch (error) {
    if (generation === campaign.version) failure(error, "campaign-compose-error");
  } finally {
    campaign.sending = false;
    updateSubmit();
  }
}

function campaignCounts(item) {
  return "В очереди: " + item.queued_count + " · Отправляется: " +
    item.sending_count + " · Доставлено: " + item.sent_count +
    " · Ошибок: " + item.failed_count;
}

function renderHistory() {
  const node = el("campaign-history-list");
  node.innerHTML = campaign.history.map((item) =>
    '<button type="button" class="campaign-history-item" data-campaign-id="' + item.id + '">' +
    '<span class="campaign-history-title">Кампания #' + item.id +
    '<time>' + html(new Date(item.created_at).toLocaleString("ru-RU")) + "</time></span>" +
    '<span class="campaign-history-state">' + html(statuses[item.status] || item.status) +
    " · " + item.recipient_count + " адресатов</span>" +
    '<span class="campaign-history-text">' + html(item.text || "") + "</span>" +
    '<small>' + html(campaignCounts(item)) + "</small></button>").join("");
  if (!campaign.history.length && !campaign.historyBusy)
    node.innerHTML = '<p class="campaign-empty">Кампаний пока нет.</p>';
  setHidden("campaign-history-more", !campaign.cursor);
  el("campaign-history-more").disabled = campaign.historyBusy;
}

async function loadHistory(reset = false) {
  if (campaign.screen !== "history" || !campaign.client ||
      (campaign.historyBusy && !reset)) return;
  const token = ++campaign.historyGeneration;
  campaign.historyController?.abort();
  const controller = new AbortController();
  campaign.historyController = controller;
  if (reset) {
    campaign.history = [];
    campaign.cursor = null;
  }
  campaign.historyBusy = true;
  message("campaign-history-status", "Загружаем кампании…");
  renderHistory();
  try {
    const result = await campaign.client.campaignList({
      cursor: campaign.cursor, limit: 50, signal: controller.signal,
    });
    if (token !== campaign.historyGeneration || campaign.screen !== "history") return;
    const byId = new Map(campaign.history.map((item) => [item.id, item]));
    for (const item of result.items || []) byId.set(item.id, item);
    campaign.history = [...byId.values()].sort((a, b) => b.id - a.id);
    campaign.cursor = result.next_cursor ?? null;
    message("campaign-history-status", "");
  } catch (error) {
    if (token === campaign.historyGeneration) failure(error, "campaign-history-status");
  } finally {
    if (token === campaign.historyGeneration) {
      campaign.historyBusy = false;
      renderHistory();
    }
  }
}

function renderDetail() {
  const detail = campaign.detail;
  setHidden("campaign-detail", !detail);
  if (!detail) return;
  message("campaign-detail-id", "#" + detail.id);
  message("campaign-detail-summary", (statuses[detail.status] || detail.status) +
    " · Адресатов: " + detail.recipient_count + " · " + campaignCounts(detail));
  el("campaign-detail-recipients").innerHTML = detail.recipients.map((recipient) =>
    '<div class="campaign-recipient"><span><strong>' + html(recipient.full_name) +
    '</strong><small>' + html(recipient.study_group_name || "Без группы") +
    '</small></span><span>' + html(statuses[recipient.status] || recipient.status) +
    (recipient.error_code ? '<small class="campaign-error">' + html(recipient.error_code) + "</small>" : "") +
    (recipient.retry_count ? '<small>Повторов: ' + Number(recipient.retry_count) + "</small>" : "") +
    "</span></div>").join("");
  setHidden("campaign-retry-button", !detail.failed_count);
  el("campaign-retry-button").disabled = campaign.retryBusy;
}

async function loadDetail(id) {
  if (campaign.screen !== "history" || !campaign.client) return;
  const token = ++campaign.detailGeneration;
  campaign.detailController?.abort();
  const controller = new AbortController();
  campaign.detailController = controller;
  message("campaign-detail-status", "Загружаем доставки…");
  try {
    const detail = await campaign.client.campaignDetail(id, { signal: controller.signal });
    if (token !== campaign.detailGeneration || campaign.screen !== "history") return;
    campaign.detail = detail;
    renderDetail();
    message("campaign-detail-status", "");
  } catch (error) {
    if (token === campaign.detailGeneration) failure(error, "campaign-detail-status");
  }
}

async function retryFailed() {
  if (!campaign.client || campaign.screen !== "history" ||
      !campaign.detail?.failed_count || campaign.retryBusy) return;
  const id = campaign.detail.id;
  campaign.retryBusy = true;
  renderDetail();
  message("campaign-detail-status", "Повторяем только неуспешные доставки…");
  try {
    const summary = await campaign.client.campaignRetry(id);
    if (campaign.screen !== "history") return;
    message("campaign-detail-status", "Поставлены в очередь только неуспешные доставки.");
    const index = campaign.history.findIndex((item) => item.id === id);
    if (index >= 0) campaign.history[index] = summary;
    renderHistory();
    await loadDetail(id);
  } catch (error) {
    failure(error, "campaign-detail-status");
  } finally {
    campaign.retryBusy = false;
    renderDetail();
  }
}

export function refreshCampaignsFromEvent() {
  if (campaign.screen !== "history" || !campaign.client) return;
  clearTimeout(campaign.eventTimer);
  campaign.eventTimer = window.setTimeout(() => {
    if (campaign.screen !== "history") return;
    const id = campaign.detail?.id;
    void loadHistory(true);
    if (id) void loadDetail(id);
  }, 350);
}

export function bindCampaignControls() {
  el("campaign-group-options")?.addEventListener("change", (event) => {
    const id = Number(event.target.dataset.campaignGroup);
    if (!id) return;
    if (event.target.checked) campaign.groupIds.add(id);
    else campaign.groupIds.delete(id);
    invalidatePreview();
  });
  el("campaign-users")?.addEventListener("change", (event) => {
    const id = Number(event.target.dataset.campaignUser);
    if (!id) return;
    if (event.target.checked) campaign.userIds.add(id);
    else campaign.userIds.delete(id);
    invalidatePreview();
  });
  el("campaign-user-search")?.addEventListener("input", (event) => {
    campaign.search = event.target.value;
    clearTimeout(campaign.searchTimer);
    campaign.searchTimer = window.setTimeout(() => void loadUsers(true), SEARCH_DEBOUNCE_MS);
  });
  el("campaign-user-filter")?.addEventListener("change", (event) => {
    campaign.filterGroup = event.target.value;
    clearTimeout(campaign.searchTimer);
    void loadUsers(true);
  });
  el("campaign-more-users")?.addEventListener("click", () => void loadUsers());
  el("campaign-text")?.addEventListener("input", (event) => {
    campaign.text = event.target.value;
    invalidatePreview();
  });
  el("campaign-preview-button")?.addEventListener("click", () => void previewCampaign());
  el("campaign-confirm-button")?.addEventListener("click", () => void confirmCampaign());
  el("campaign-history-refresh")?.addEventListener("click", () => {
    void loadHistory(true);
    if (campaign.detail?.id) void loadDetail(campaign.detail.id);
  });
  el("campaign-history-new")?.addEventListener("click", () =>
    openCampaignCompose(campaign.client, campaign.onAuthError));
  el("campaign-history-more")?.addEventListener("click", () => void loadHistory());
  el("campaign-history-list")?.addEventListener("click", (event) => {
    const row = event.target.closest("[data-campaign-id]");
    if (row) void loadDetail(Number(row.dataset.campaignId));
  });
  el("campaign-retry-button")?.addEventListener("click", () => void retryFailed());
}
