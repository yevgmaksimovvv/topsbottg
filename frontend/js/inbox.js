import { SEARCH_DEBOUNCE_MS } from "./constants.js";
import { setToast, state } from "./store.js";
import { telegramWebApp } from "./telegram.js";
import { escapeHtml } from "./utils.js";

const inbox = {
  client: null,
  conversations: [],
  nextCursor: null,
  listRequest: 0,
  listLoading: false,
  listError: "",
  listController: null,
  summaryController: null,
  summaryRequest: 0,
  historyController: null,
  openController: null,
  openRequest: 0,
  readController: null,
  readingId: null,
  query: "",
  group: "",
  current: null,
  messages: [],
  historyCursor: null,
  historyRequest: 0,
  historyLoading: false,
  historyError: "",
  readThrough: null,
  replyTo: null,
  unread: 0,
  searchTimer: null,
  opener: null,
  backHandler: null,
  sending: false,
  openingUser: false,
};
let bound = false;

const el = (id) => document.getElementById(id);
const dialog = () => el("inbox-dialog");
const isOpen = () => Boolean(dialog()?.open);
const text = (value) => escapeHtml(String(value ?? ""));
const time = (value) => {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
};
const label = (user) => user?.full_name || "Пользователь";
const groupLabel = (user) => user?.study_group_name || "Группа не указана";
const statusText = {
  queued: "В очереди",
  sending: "Отправляется",
  sent: "Отправлено",
  failed: "Ошибка отправки",
};
const key = () => window.crypto?.randomUUID?.() || String(Date.now()) + "-" + Math.random().toString(36).slice(2);

export function isInboxEnabled() {
  return Boolean(inbox.client);
}

function status(nodeId, message) {
  const node = el(nodeId);
  if (node) node.textContent = message || "";
}

function isAbort(error) {
  return error?.name === "AbortError";
}

function handleRequestError(error) {
  if (error?.status === 401 || error?.status === 403) {
    disableInboxAccess();
    return true;
  }
  return false;
}

function cancelRequests() {
  for (const prop of ["listController", "summaryController", "historyController", "openController", "readController"]) {
    inbox[prop]?.abort();
    inbox[prop] = null;
  }
}

export function disableInboxAccess() {
  if (!inbox.client) return;
  configureInboxClient(null);
  state.adminRole = null;
  el("admins-open")?.classList.add("hidden");
  setToast("Доступ к переписке прекращён. Откройте Mini App заново после проверки прав.", "warning");
}

function paintBadge() {
  const badge = el("inbox-unread");
  if (!badge) return;
  badge.textContent = inbox.unread > 99 ? "99+" : String(inbox.unread);
  badge.classList.toggle("hidden", !inbox.unread);
}

async function loadSummary() {
  if (!inbox.client) return;
  const requestId = ++inbox.summaryRequest;
  inbox.summaryController?.abort();
  const controller = new AbortController();
  inbox.summaryController = controller;
  const client = inbox.client;
  try {
    const response = await client.summary({ signal: controller.signal });
    if (requestId !== inbox.summaryRequest || client !== inbox.client || controller.signal.aborted) return;
    inbox.unread = Math.max(0, Number(response?.unread_count) || 0);
    paintBadge();
  } catch (error) {
    if (!isAbort(error) && requestId === inbox.summaryRequest) handleRequestError(error);
  } finally {
    if (inbox.summaryController === controller) inbox.summaryController = null;
  }
}

function renderGroups() {
  const control = el("inbox-group");
  if (!control) return;
  control.replaceChildren(new Option("Все группы", ""));
  for (const group of state.studyGroups) {
    control.add(new Option(group.code, String(group.id)));
  }
  control.value = inbox.group;
  if (control.selectedIndex === -1) {
    control.add(new Option("Выбранная группа", inbox.group));
    control.value = inbox.group;
  }
}

function renderConversations() {
  const list = el("inbox-conversations");
  if (!list) return;
  list.innerHTML = inbox.conversations.map((item) => {
    const user = item.user || {};
    const last = item.last_message;
    const snippet = typeof last === "string" ? last
      : last?.text || last?.caption || (last?.kind ? "Вложение" : "Сообщений пока нет");
    const unread = Math.max(0, Number(item.unread_count) || 0);
    return '<button type="button" class="inbox-conversation" data-inbox-conversation="' + Number(item.id) + '">' +
      '<span class="inbox-row-heading"><strong>' + text(label(user)) + '</strong>' +
      '<time>' + text(time(item.last_message_at)) + '</time></span>' +
      '<span class="inbox-row-subtitle">' + text(groupLabel(user)) + '</span>' +
      '<span class="inbox-row-bottom"><span class="inbox-snippet">' + text(snippet) + '</span>' +
      (unread ? '<span class="inbox-count" aria-label="Непрочитанных: ' + unread + '">' + (unread > 99 ? "99+" : unread) + '</span>' : "") +
      '</span></button>';
  }).join("");
  const more = el("inbox-more");
  more?.classList.toggle("hidden", !inbox.nextCursor);
  if (more) more.disabled = inbox.listLoading;
  el("inbox-list-retry")?.classList.toggle("hidden", !inbox.listError);
  status("inbox-list-status", inbox.listLoading
    ? "Загружаем диалоги…"
    : inbox.listError || (inbox.conversations.length ? "" : "Диалогов пока нет."));
}

async function loadConversations(reset = false) {
  if (!inbox.client || (inbox.listLoading && !reset)) return;
  const generation = ++inbox.listRequest;
  inbox.listController?.abort();
  const controller = new AbortController();
  inbox.listController = controller;
  const client = inbox.client;
  const query = inbox.query;
  const group = inbox.group;
  const cursor = reset ? null : inbox.nextCursor;
  if (reset) {
    inbox.conversations = [];
    inbox.nextCursor = null;
  }
  inbox.listLoading = true;
  inbox.listError = "";
  renderConversations();
  try {
    const response = await client.conversations({ query, study_group_id: group, cursor, limit: 50, signal: controller.signal });
    if (generation !== inbox.listRequest || controller.signal.aborted || client !== inbox.client || !isOpen()) return;
    const byId = new Map((reset ? [] : inbox.conversations).map((item) => [item.id, item]));
    for (const item of response.items || []) byId.set(item.id, item);
    inbox.conversations = [...byId.values()];
    inbox.nextCursor = response.next_cursor ?? null;
  } catch (error) {
    if (generation === inbox.listRequest && client === inbox.client && !isAbort(error)) {
      if (!handleRequestError(error)) inbox.listError = error?.message || "Не удалось загрузить диалоги.";
    }
  } finally {
    if (inbox.listController === controller) inbox.listController = null;
    if (generation === inbox.listRequest && client === inbox.client) {
      inbox.listLoading = false;
      renderConversations();
    }
  }
}

function showList() {
  ++inbox.historyRequest;
  inbox.historyController?.abort();
  inbox.readController?.abort();
  inbox.current = null;
  inbox.messages = [];
  inbox.replyTo = null;
  el("inbox-list-screen").hidden = false;
  el("inbox-history-screen").hidden = true;
  el("inbox-back").hidden = true;
  el("inbox-title").textContent = "Сообщения";
  el("inbox-search")?.focus();
  void loadConversations(true);
  void loadSummary();
}

function renderReply() {
  const node = el("inbox-reply");
  if (!node) return;
  node.replaceChildren();
  if (inbox.replyTo == null) return;
  const message = inbox.messages.find((item) => item.id === inbox.replyTo);
  if (!message) {
    inbox.replyTo = null;
    return;
  }
  const line = document.createElement("span");
  line.textContent = "Ответ на: " + (message.text || message.caption || "сообщение").slice(0, 90);
  const button = document.createElement("button");
  button.type = "button";
  button.id = "inbox-clear-reply";
  button.textContent = "×";
  button.setAttribute("aria-label", "Отменить ответ");
  node.append(line, button);
}

function renderHistory({ preserveScroll = false } = {}) {
  const list = el("inbox-history");
  if (!list) return;
  const oldHeight = list.scrollHeight;
  const oldTop = list.scrollTop;
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 75;
  const unread = Math.max(0, Number(inbox.current?.unread_count) || 0);
  const inbound = inbox.messages.filter((message) => message.direction === "incoming");
  const unreadStart = unread > 0 ? inbound.at(-Math.min(unread, inbound.length))?.id : null;
  list.innerHTML = inbox.messages.map((message) => {
    const outgoing = message.direction === "outgoing";
    const delivery = outgoing && message.delivery_status ? message.delivery_status : null;
    const body = message.text || message.caption || (message.attachment ? "Вложение: " + (message.kind || "файл") : "");
    const isUnread = unreadStart === message.id;
    return (isUnread ? '<div class="inbox-unread-divider">Непрочитанные сообщения</div>' : "") +
      '<article class="inbox-message ' + (outgoing ? "is-outgoing" : "is-incoming") + '" data-inbox-message="' + text(message.id) + '">' +
      '<span class="inbox-message-author">' + (outgoing
        ? "Администратор" + (message.author_admin_telegram_id != null ? " · ID " + text(message.author_admin_telegram_id) : "")
        : "Пользователь") + '</span>' +
      (message.reply_to_telegram_message_id != null
        ? '<span class="inbox-message-reply">Ответ на Telegram #' + text(message.reply_to_telegram_message_id) + '</span>' : "") +
      '<span class="inbox-message-body">' + text(body) + '</span>' +
      '<span class="inbox-message-meta"><time>' + text(time(message.created_at)) + '</time>' +
      (delivery ? '<span class="' + (delivery === "failed" ? "inbox-failed" : "") + '">' + text(statusText[delivery] || delivery) + '</span>' : "") +
      '</span>' +
      '<span class="inbox-message-actions"><button type="button" data-inbox-reply="' + text(message.id) + '">Ответить</button>' +
      (delivery === "failed" ? '<button type="button" data-inbox-retry="' + text(message.id) + '">' +
        (message._sendRequest ? "Повторить" : "Отправить заново") + '</button>' : "") +
      '</span></article>' +
      (inbox.readThrough === message.id ? '<div class="inbox-read-marker">Прочитано до этого сообщения</div>' : "");
  }).join("");
  el("inbox-older")?.classList.toggle("hidden", !inbox.historyCursor);
  if (el("inbox-older")) el("inbox-older").disabled = inbox.historyLoading;
  status("inbox-history-status", inbox.historyLoading
    ? "Загружаем сообщения…" : inbox.historyError || (inbox.messages.length ? "" : "Сообщений пока нет."));
  renderReply();
  if (preserveScroll) list.scrollTop = oldTop + list.scrollHeight - oldHeight;
  else if (atBottom || !oldHeight) list.scrollTop = list.scrollHeight;
  else list.scrollTop = oldTop;
}

async function loadMessages({ older = false, refresh = false } = {}) {
  if (!inbox.client || !inbox.current || (inbox.historyLoading && older)) return;
  const generation = ++inbox.historyRequest;
  inbox.historyController?.abort();
  const controller = new AbortController();
  inbox.historyController = controller;
  const client = inbox.client;
  const conversationId = inbox.current.id;
  const before = older ? inbox.historyCursor : null;
  if (older && !before) return;
  inbox.historyLoading = true;
  inbox.historyError = "";
  renderHistory();
  const list = el("inbox-history");
  const oldScrollHeight = list.scrollHeight;
  const oldTop = list.scrollTop;
  const wasAtBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 75;
  try {
    const response = await client.messages(conversationId, { before, limit: 50, signal: controller.signal });
    if (generation !== inbox.historyRequest || controller.signal.aborted || client !== inbox.client || inbox.current?.id !== conversationId || !isOpen()) return;
    const byId = new Map((older || refresh ? inbox.messages : []).map((item) => [item.id, item]));
    for (const item of response.items || []) byId.set(item.id, item);
    inbox.messages = [...byId.values()].sort((a, b) => Number(a.id) - Number(b.id));
    if (older || !refresh) inbox.historyCursor = response.next_cursor ?? null;
    renderHistory();
    if (older) list.scrollTop = oldTop + list.scrollHeight - oldScrollHeight;
    else if (wasAtBottom || !refresh) list.scrollTop = list.scrollHeight;
    else list.scrollTop = oldTop;
    void markRead();
  } catch (error) {
    if (generation === inbox.historyRequest && client === inbox.client && !isAbort(error)) {
      if (!handleRequestError(error)) inbox.historyError = error?.message || "Не удалось загрузить сообщения.";
    }
  } finally {
    if (inbox.historyController === controller) inbox.historyController = null;
    if (generation === inbox.historyRequest && client === inbox.client) {
      inbox.historyLoading = false;
      status("inbox-history-status", inbox.historyError || (inbox.messages.length ? "" : "Сообщений пока нет."));
      el("inbox-older").disabled = false;
      el("inbox-older")?.classList.toggle("hidden", !inbox.historyCursor);
    }
  }
}

function lastVisibleIncomingId() {
  const history = el("inbox-history");
  if (!isOpen() || el("inbox-history-screen")?.hidden ||
      document.visibilityState !== "visible" || !document.hasFocus() || !history) return null;
  const viewport = history.getBoundingClientRect();
  if (!viewport.width || !viewport.height) return null;
  let last = null;
  for (const message of history.querySelectorAll(".inbox-message.is-incoming")) {
    const rect = message.getBoundingClientRect();
    const visibleHeight = Math.min(rect.bottom, viewport.bottom) - Math.max(rect.top, viewport.top);
    if (visibleHeight >= Math.min(24, rect.height / 2)) {
      const id = Number(message.dataset.inboxMessage);
      if (Number.isInteger(id) && id > 0) last = Math.max(last || 0, id);
    }
  }
  return last;
}

async function markRead() {
  if (!inbox.client || !inbox.current) return;
  const visibleId = lastVisibleIncomingId();
  if (!visibleId || visibleId <= Number(inbox.readThrough || 0) ||
      visibleId <= Number(inbox.readingId || 0)) return;
  const client = inbox.client;
  const id = inbox.current.id;
  const controller = new AbortController();
  inbox.readController?.abort();
  inbox.readController = controller;
  inbox.readingId = visibleId;
  try {
    const result = await client.read(id, visibleId, { signal: controller.signal });
    if (controller.signal.aborted || client !== inbox.client || inbox.current?.id !== id) return;
    if (Number(result.last_read_message_id) < Number(inbox.readThrough || 0)) return;
    inbox.readThrough = result.last_read_message_id;
    inbox.current.unread_count = result.unread_count;
    renderHistory();
    void loadSummary();
  } catch (error) {
    if (!isAbort(error)) handleRequestError(error);
  } finally {
    if (inbox.readController === controller) inbox.readController = null;
    if (inbox.readingId === visibleId) inbox.readingId = null;
  }
}

async function openConversation(conversation) {
  inbox.historyController?.abort();
  inbox.readController?.abort();
  inbox.current = conversation;
  inbox.messages = [];
  inbox.historyCursor = null;
  inbox.readThrough = null;
  inbox.readingId = null;
  inbox.historyError = "";
  inbox.replyTo = null;
  ++inbox.historyRequest;
  el("inbox-list-screen").hidden = true;
  el("inbox-history-screen").hidden = false;
  el("inbox-back").hidden = false;
  el("inbox-title").textContent = label(conversation.user);
  el("inbox-text").value = "";
  renderHistory();
  el("inbox-text").focus();
  await loadMessages();
}

async function openForUser(userId) {
  if (!inbox.client || inbox.openingUser) return;
  inbox.openingUser = true;
  const user = state.users.find((item) => Number(item.id) === Number(userId));
  const generation = ++inbox.openRequest;
  inbox.openController?.abort();
  const controller = new AbortController();
  inbox.openController = controller;
  const client = inbox.client;
  try {
    if (!isOpen()) openInbox();
    const result = await client.open(Number(userId), { signal: controller.signal });
    if (generation !== inbox.openRequest || controller.signal.aborted || client !== inbox.client || !isOpen()) return;
    const existing = inbox.conversations.find((item) => Number(item.id) === Number(result.conversation_id));
    await openConversation(existing || { id: result.conversation_id,
      user: { id: userId, full_name: user?.full_name, study_group_name: user?.study_group?.code },
      unread_count: 0 });
  } catch (error) {
    if (!isAbort(error) && generation === inbox.openRequest && client === inbox.client &&
        !handleRequestError(error))
      status("inbox-list-status", error?.message || "Не удалось открыть диалог.");
  } finally {
    if (inbox.openController === controller) inbox.openController = null;
    if (generation === inbox.openRequest) inbox.openingUser = false;
  }
}

async function deliverPending(pending) {
  if (!inbox.client || !inbox.current || inbox.sending ||
    !pending._sendRequest || pending.delivery_status === "sending" ||
    Number(pending.conversation_id) !== Number(inbox.current.id)) return;

  const id = inbox.current.id;
  const client = inbox.client;
  const send = el("inbox-send");
  inbox.sending = true;
  send.disabled = true;
  pending.delivery_status = "sending";
  renderHistory();
  try {
    const { message } = await client.send(id, pending._sendRequest);
    if (client !== inbox.client || inbox.current?.id !== id) return;
    inbox.messages = inbox.messages.filter((item) => item !== pending && item.id !== message.id);
    inbox.messages.push(message);
    inbox.messages.sort((a, b) => Number(a.id) - Number(b.id));
    status("inbox-compose-status", "");
    renderHistory();
  } catch (error) {
    if (client === inbox.client && inbox.current?.id === id && !handleRequestError(error)) {
      pending.delivery_status = "failed";
      status("inbox-compose-status", error?.message || "Не удалось отправить. Сообщение можно повторить.");
      renderHistory();
    }
  } finally {
    if (client === inbox.client) {
      inbox.sending = false;
      send.disabled = false;
    }
  }
}

function sendMessage(textValue, replyTo = null) {
  if (!inbox.client || !inbox.current || inbox.sending || !textValue.trim()) return;
  const messageKey = key();
  const request = Object.freeze({
    text: textValue,
    ...(replyTo != null ? { reply_to_message_id: replyTo } : {}),
    idempotency_key: messageKey,
  });
  const pending = {
    id: "pending-" + messageKey,
    conversation_id: inbox.current.id,
    direction: "outgoing",
    kind: "text",
    text: textValue,
    caption: null,
    created_at: new Date().toISOString(),
    author_admin_telegram_id: null,
    telegram_message_id: null,
    reply_to_telegram_message_id: null,
    delivery_status: "queued",
    delivery_error_code: null,
    attachment: null,
    _sendRequest: request,
  };
  inbox.messages.push(pending);
  el("inbox-text").value = "";
  inbox.replyTo = null;
  renderHistory();
  void deliverPending(pending);
}

function openInbox() {
  if (!inbox.client || isOpen()) return;
  inbox.opener = document.activeElement;
  dialog().showModal();
  const back = telegramWebApp()?.BackButton;
  inbox.backHandler = closeInbox;
  back?.onClick?.(inbox.backHandler);
  back?.show?.();
  renderGroups();
  showList();
}

function closeInbox() {
  if (isOpen()) {
    dialog().close();
    cleanup();
  }
}

function cleanup() {
  ++inbox.listRequest;
  ++inbox.historyRequest;
  ++inbox.summaryRequest;
  ++inbox.openRequest;
  cancelRequests();
  inbox.openingUser = false;
  clearTimeout(inbox.searchTimer);
  const back = telegramWebApp()?.BackButton;
  if (inbox.backHandler) back?.offClick?.(inbox.backHandler);
  inbox.backHandler = null;
  back?.hide?.();
  if (inbox.opener?.isConnected && !inbox.opener.closest(".hidden, [hidden]"))
    inbox.opener.focus();
  inbox.opener = null;
  inbox.readingId = null;
}

export function configureInboxClient(client) {
  if (client !== null && (!client || ["summary", "conversations", "open", "messages", "send", "read"].some((method) => typeof client[method] !== "function")))
    throw new TypeError("Inbox client does not implement contract #24");
  if (isOpen()) closeInbox();
  inbox.client = client;
  inbox.unread = 0;
  if (!client) {
    cancelRequests();
    ++inbox.summaryRequest;
    inbox.conversations = [];
    inbox.messages = [];
    inbox.current = null;
    inbox.nextCursor = null;
    inbox.historyCursor = null;
    inbox.listError = "";
    inbox.historyError = "";
    el("inbox-conversations")?.replaceChildren();
    el("inbox-history")?.replaceChildren();
  }
  paintBadge();
  el("inbox-open")?.classList.toggle("hidden", !client);
  const list = el("users-list");
  if (list) {
    list.dataset.userIds = "";
    void import("./render-users.js").then(({ renderUsers }) => renderUsers());
  }
  if (client) void loadSummary();
}

export function bindInbox() {
  if (bound) return;
  bound = true;
  el("inbox-open")?.addEventListener("click", openInbox);
  el("inbox-close")?.addEventListener("click", closeInbox);
  el("inbox-back")?.addEventListener("click", showList);
  dialog()?.addEventListener("close", cleanup);
  el("inbox-search")?.addEventListener("input", (event) => {
    inbox.query = event.target.value;
    ++inbox.listRequest;
    clearTimeout(inbox.searchTimer);
    inbox.searchTimer = window.setTimeout(() => void loadConversations(true), SEARCH_DEBOUNCE_MS);
  });
  el("inbox-group")?.addEventListener("change", (event) => {
    inbox.group = event.target.value;
    ++inbox.listRequest;
    clearTimeout(inbox.searchTimer);
    void loadConversations(true);
  });
  el("inbox-more")?.addEventListener("click", () => void loadConversations());
  el("inbox-list-retry")?.addEventListener("click", () => void loadConversations(true));
  el("inbox-older")?.addEventListener("click", () => void loadMessages({ older: true }));
  el("inbox-refresh")?.addEventListener("click", () => void loadMessages({ refresh: true }));
  el("inbox-conversations")?.addEventListener("click", (event) => {
    const row = event.target.closest("[data-inbox-conversation]");
    if (!row) return;
    const item = inbox.conversations.find((conversation) => conversation.id === Number(row.dataset.inboxConversation));
    if (item) void openConversation(item);
  });
  el("inbox-history")?.addEventListener("click", (event) => {
    const reply = event.target.closest("[data-inbox-reply]");
    const retry = event.target.closest("[data-inbox-retry]");
    if (reply) {
      const message = inbox.messages.find((item) => String(item.id) === reply.dataset.inboxReply);
      if (message && Number.isInteger(Number(message.id))) {
        inbox.replyTo = Number(message.id);
        renderReply();
        el("inbox-text").focus();
      }
    }
    if (retry) {
      const message = inbox.messages.find((item) => String(item.id) === retry.dataset.inboxRetry);
      if (message?.delivery_status === "failed") {
        if (message._sendRequest) void deliverPending(message);
        else if (message.text) sendMessage(message.text);
      }
    }
  });
  el("inbox-history")?.addEventListener("scroll", (event) => {
    const list = event.target;
    if (list.scrollTop < 48 && inbox.historyCursor && !inbox.historyLoading)
      void loadMessages({ older: true });
    void markRead();
  }, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void markRead();
  });
  window.addEventListener("focus", () => void markRead());
  el("inbox-reply")?.addEventListener("click", (event) => {
    if (!event.target.closest("#inbox-clear-reply")) return;
    inbox.replyTo = null;
    renderReply();
    el("inbox-text").focus();
  });
  el("inbox-composer")?.addEventListener("submit", (event) => {
    event.preventDefault();
    status("inbox-compose-status", "");
    void sendMessage(el("inbox-text").value, inbox.replyTo);
  });
  document.addEventListener("click", (event) => {
    const write = event.target.closest("[data-inbox-write]");
    if (write && inbox.client) void openForUser(write.dataset.inboxWrite);
  });
}

export function refreshInboxFromEvent(conversationId = null, { reconnect = false } = {}) {
  if (!inbox.client) return;
  void loadSummary();
  if (!isOpen()) return;
  if (!el("inbox-list-screen")?.hidden) {
    void loadConversations(true);
  } else if (inbox.current && (reconnect || conversationId == null ||
      Number(inbox.current.id) === Number(conversationId))) {
    void loadMessages({ refresh: true });
  }
}
