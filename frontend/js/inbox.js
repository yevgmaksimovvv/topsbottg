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
  newQuery: "",
  newGroup: "",
  newUsers: [],
  newOffset: 0,
  newHasMore: false,
  newLoading: false,
  newError: "",
  newRequest: 0,
  newController: null,
  newSearchTimer: null,
  current: null,
  messages: [],
  historyCursor: null,
  historyRequest: 0,
  historyLoading: false,
  historyError: "",
  readThrough: null,
  seenIncoming: new Set(),
  authorLabels: new Map(),
  replyTo: null,
  unread: 0,
  searchTimer: null,
  opener: null,
  backHandler: null,
  sending: false,
  openingUser: false,
  draftFile: null,
  draftPending: null,
  mediaUrls: new Map(),
  mediaRequests: new Map(),
  mediaErrors: new Map(),
  mediaObserver: null,
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
const statusText = {
  queued: "В очереди",
  sending: "Отправляется",
  sent: "Отправлено",
  failed: "Ошибка отправки",
};
const key = () => window.crypto?.randomUUID?.() || String(Date.now()) + "-" + Math.random().toString(36).slice(2);
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
const PHOTO_TYPES = new Set(["image/jpeg", "image/png"]);
const FILE_TYPES = new Set([
  "application/pdf", "application/zip", "text/plain",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ...PHOTO_TYPES, "audio/ogg",
]);
const fileSize = (size) => size == null ? "" :
  (size < 1024 ? size + " Б" :
    size < 1024 * 1024 ? (size / 1024).toFixed(1) + " КБ" :
      (size / (1024 * 1024)).toFixed(1) + " МБ");
const fileKind = (file) => PHOTO_TYPES.has(file.type) ? "photo" :
  file.type === "audio/ogg" ? "voice" : "document";

function clearMedia() {
  inbox.mediaObserver?.disconnect();
  inbox.mediaObserver = null;
  for (const controller of inbox.mediaRequests.values()) controller.abort();
  inbox.mediaRequests.clear();
  for (const url of inbox.mediaUrls.values()) URL.revokeObjectURL(url);
  inbox.mediaUrls.clear();
  inbox.mediaErrors.clear();
  const viewer = el("inbox-photo-viewer");
  if (viewer?.open) viewer.close();
  const image = el("inbox-photo-full");
  if (image) image.removeAttribute("src");
}

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
  clearMedia();
  for (const prop of ["listController", "summaryController", "historyController", "openController", "readController", "newController"]) {
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
  for (const [id, selected] of [["inbox-group", inbox.group], ["inbox-new-group", inbox.newGroup]]) {
    const control = el(id);
    if (!control) continue;
    control.replaceChildren(new Option("Все группы", ""));
    for (const group of state.studyGroups) control.add(new Option(group.code, String(group.id)));
    control.value = selected;
    if (control.selectedIndex === -1) {
      control.add(new Option("Выбранная группа", selected));
      control.value = selected;
    }
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
      (user.study_group_name ? '<span class="inbox-row-subtitle">' + text(user.study_group_name) + '</span>' : "") +
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
    const current = inbox.conversations.find((item) => Number(item.id) === Number(inbox.current?.id));
    if (current) {
      inbox.current.unread_count = current.unread_count;
      renderHistory();
      void markRead();
    }
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

function renderNewUsers() {
  const list = el("inbox-new-users");
  if (!list) return;
  list.innerHTML = inbox.newUsers.map((user) =>
    '<button type="button" class="inbox-new-user" data-inbox-new-user="' + Number(user.id) + '">' +
    '<span class="inbox-row-heading"><strong>' + text(user.full_name) + '</strong></span>' +
    (user.study_group?.code ? '<span class="inbox-row-subtitle">' + text(user.study_group.code) + '</span>' : "") +
    '</button>'
  ).join("");
  el("inbox-new-more")?.classList.toggle("hidden", !inbox.newHasMore);
  if (el("inbox-new-more")) el("inbox-new-more").disabled = inbox.newLoading;
  el("inbox-new-retry")?.classList.toggle("hidden", !inbox.newError);
  status("inbox-new-status", inbox.newLoading
    ? "Ищем пользователей…"
    : inbox.newError || (inbox.newUsers.length ? "" : "Пользователи не найдены."));
}

async function loadNewUsers(reset = false) {
  if (!inbox.client || (inbox.newLoading && !reset) || !isOpen()) return;
  const generation = ++inbox.newRequest;
  inbox.newController?.abort();
  const controller = new AbortController();
  inbox.newController = controller;
  const client = inbox.client;
  const query = inbox.newQuery;
  const group = inbox.newGroup;
  if (reset) {
    inbox.newUsers = [];
    inbox.newOffset = 0;
    inbox.newHasMore = false;
  }
  inbox.newLoading = true;
  inbox.newError = "";
  renderNewUsers();
  try {
    const page = await client.users({
      search: query, study_group_id: group, offset: inbox.newOffset, limit: 50,
      signal: controller.signal,
    });
    if (generation !== inbox.newRequest || controller.signal.aborted ||
        client !== inbox.client || el("inbox-new-screen")?.hidden || !isOpen()) return;
    const seen = new Set(inbox.newUsers.map((user) => Number(user.id)));
    for (const user of page.items || []) {
      if (!seen.has(Number(user.id))) inbox.newUsers.push(user);
    }
    inbox.newOffset = Number(page.offset || 0) + (page.items || []).length;
    inbox.newHasMore = Boolean(page.has_more);
  } catch (error) {
    if (generation === inbox.newRequest && !isAbort(error) && client === inbox.client) {
      if (!handleRequestError(error)) inbox.newError = error?.message || "Не удалось найти пользователей.";
    }
  } finally {
    if (inbox.newController === controller) inbox.newController = null;
    if (generation === inbox.newRequest && client === inbox.client) {
      inbox.newLoading = false;
      renderNewUsers();
    }
  }
}

function showNewDialog() {
  ++inbox.listRequest;
  inbox.listController?.abort();
  inbox.newQuery = "";
  inbox.newGroup = "";
  inbox.newUsers = [];
  inbox.newOffset = 0;
  inbox.newHasMore = false;
  el("inbox-list-screen").hidden = true;
  el("inbox-history-screen").hidden = true;
  el("inbox-new-screen").hidden = false;
  el("inbox-new").hidden = true;
  el("inbox-back").hidden = false;
  el("inbox-title").textContent = "Новый диалог";
  el("inbox-new-search").value = "";
  renderGroups();
  void loadNewUsers(true);
  el("inbox-new-search").focus();
}

function showList() {
  ++inbox.historyRequest;
  ++inbox.newRequest;
  ++inbox.openRequest;
  inbox.openController?.abort();
  inbox.openingUser = false;
  inbox.newController?.abort();
  clearTimeout(inbox.newSearchTimer);
  inbox.historyController?.abort();
  inbox.readController?.abort();
  inbox.current = null;
  inbox.messages = [];
  inbox.seenIncoming.clear();
  inbox.replyTo = null;
  el("inbox-list-screen").hidden = false;
  el("inbox-history-screen").hidden = true;
  el("inbox-new-screen").hidden = true;
  el("inbox-new").hidden = false;
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

function messageClock(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(date);
}

function mediaMarkup(message) {
  const attachment = message.attachment;
  if (!attachment) return "";
  const id = Number(message.id);
  const kind = attachment.kind || message.kind;
  const name = attachment.filename || (kind === "voice" ? "Голосовое сообщение" :
    kind === "photo" ? "Фотография" : "Документ");
  const size = fileSize(attachment.size);
  const meta = size ? " · " + size : "";
  const unavailable = !attachment.available || !Number.isSafeInteger(id) || id <= 0;
  const error = inbox.mediaErrors.get(id);
  let content;
  if (kind === "photo" && !unavailable) {
    content = '<button type="button" class="inbox-photo-button" data-inbox-view-photo="' + id +
      '" aria-label="Открыть фотографию">' +
      '<img data-inbox-photo="' + id + '" alt="Фотография" />' +
      '<span class="inbox-photo-placeholder">Фотография' + text(meta) + '</span></button>';
  } else if (kind === "voice" && !unavailable) {
    const cached = inbox.mediaUrls.get(id);
    content = cached
      ? '<audio controls preload="metadata" data-inbox-audio-player="' + id +
        '" src="' + text(cached) + '" aria-label="Голосовое сообщение"></audio>'
      : '<button type="button" class="inbox-media-link" data-inbox-load-voice="' + id +
        '">▶ Голосовое сообщение' + text(attachment.duration ? " · " + attachment.duration + " с" : "") +
        text(meta) + '</button>';
  } else if (!unavailable) {
    content = '<button type="button" class="inbox-media-link" data-inbox-download="' + id +
      '">↓ ' + text(name) + text(meta) + '</button>';
  } else {
    content = '<span class="inbox-media-unavailable">' + text(name) + text(meta) +
      (message.delivery_status === "queued" || message.delivery_status === "sending"
        ? " · Ожидает отправки" : " · Недоступно") + '</span>';
  }
  return '<div class="inbox-attachment">' + content +
    (error ? '<div class="inbox-media-error" role="status">' + text(error) +
      '<button type="button" data-inbox-reload-media="' + id + '">Повторить</button></div>' : "") +
    '</div>';
}

function syncMedia() {
  inbox.mediaObserver?.disconnect();
  inbox.mediaObserver = null;
  const photos = [...(el("inbox-messages")?.querySelectorAll("[data-inbox-photo]") || [])];
  const pending = [];
  for (const photo of photos) {
    const id = Number(photo.dataset.inboxPhoto);
    const url = inbox.mediaUrls.get(id);
    if (url) photo.src = url;
    else if (!inbox.mediaErrors.has(id) && !inbox.mediaRequests.has(id)) pending.push(photo);
  }
  if (pending.length && typeof IntersectionObserver !== "undefined") {
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        observer.unobserve(entry.target);
        const id = Number(entry.target.dataset.inboxPhoto);
        const message = inbox.messages.find((item) => Number(item.id) === id);
        if (message) void loadMedia(message);
      }
    }, { root: el("inbox-history"), rootMargin: "180px" });
    inbox.mediaObserver = observer;
    for (const photo of pending) observer.observe(photo);
  }
}

async function loadMedia(message, { download = false } = {}) {
  const id = Number(message.id);
  if (!Number.isSafeInteger(id) || id <= 0 || !message.attachment?.available ||
      !inbox.client || inbox.mediaRequests.has(id)) return;
  if (!download && inbox.mediaUrls.has(id)) return inbox.mediaUrls.get(id);
  const conversationId = inbox.current?.id;
  const client = inbox.client;
  const controller = new AbortController();
  inbox.mediaRequests.set(id, controller);
  inbox.mediaErrors.delete(id);
  try {
    const blob = await client.download(id, { signal: controller.signal });
    if (controller.signal.aborted || client !== inbox.client ||
        inbox.current?.id !== conversationId || !isOpen()) return;
    if (!(blob instanceof Blob)) throw new Error("Файл недоступен.");
    const url = URL.createObjectURL(blob);
    if (download) {
      const link = document.createElement("a");
      link.href = url;
      link.download = (message.attachment.filename || (message.kind === "photo" ? "photo.jpg" :
        message.kind === "voice" ? "voice.ogg" : "document")).replaceAll("\\", "/").split("/").at(-1);
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1500);
    } else {
      inbox.mediaUrls.set(id, url);
    }
    inbox.mediaErrors.delete(id);
    renderHistory({ preserveScroll: true });
    return url;
  } catch (error) {
    if (controller.signal.aborted || isAbort(error)) return;
    if (handleRequestError(error)) return;
    if (inbox.current?.id !== conversationId || client !== inbox.client) return;
    const label = error?.status === 413 ? "Файл превышает допустимый размер." :
      error?.status === 415 ? "Формат файла не поддерживается." :
        error?.status === 404 ? "Файл больше недоступен." :
          error?.message || "Не удалось загрузить файл.";
    inbox.mediaErrors.set(id, label);
    renderHistory({ preserveScroll: true });
  } finally {
    if (inbox.mediaRequests.get(id) === controller) inbox.mediaRequests.delete(id);
  }
}

function renderFilePreview() {
  const preview = el("inbox-file-preview");
  if (!preview) return;
  if (!inbox.draftFile) {
    preview.hidden = true;
    preview.replaceChildren();
    status("inbox-file-status", "");
    return;
  }
  const { file, kind } = inbox.draftFile;
  const name = document.createElement("span");
  name.textContent = (kind === "photo" ? "Фото: " : kind === "voice" ? "Голосовое: " : "Файл: ") +
    file.name + " · " + fileSize(file.size);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "×";
  remove.setAttribute("aria-label", "Убрать вложение");
  remove.id = "inbox-remove-file";
  preview.replaceChildren(name, remove);
  preview.hidden = false;
}

function renderHistory({ preserveScroll = false } = {}) {
  const scroll = el("inbox-history");
  const list = el("inbox-messages");
  if (!scroll || !list) return;
  const oldHeight = scroll.scrollHeight;
  const oldTop = scroll.scrollTop;
  const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 75;
  const unread = Math.max(0, Number(inbox.current?.unread_count) || 0);
  const inbound = inbox.messages.filter((message) => message.direction === "incoming");
  const unreadStart = unread > 0 && inbound.length >= unread ? inbound.at(-unread)?.id : null;
  for (const message of inbox.messages) {
    if (message.direction !== "outgoing" || message.author_admin_telegram_id == null) continue;
    const id = String(message.author_admin_telegram_id);
    if (!inbox.authorLabels.has(id)) {
      inbox.authorLabels.set(id, "Администратор " + (inbox.authorLabels.size + 1));
    }
  }
  const multipleAuthors = inbox.authorLabels.size > 1;
  list.innerHTML = inbox.messages.map((message, index) => {
    const outgoing = message.direction === "outgoing";
    const delivery = outgoing && message.delivery_status ? message.delivery_status : null;
    const body = message.text || message.caption || "";
    const isUnread = unreadStart === message.id;
    const authorId = message.author_admin_telegram_id;
    const previous = inbox.messages[index - 1];
    const showAuthor = outgoing && multipleAuthors && authorId != null &&
      (!previous || previous.direction !== "outgoing" || previous.author_admin_telegram_id !== authorId);
    const replyId = message._replyTo || null;
    const quoted = replyId != null
      ? inbox.messages.find((item) => Number(item.id) === Number(replyId))
      : message.reply_to_telegram_message_id == null ? null
        : inbox.messages.find((item) => Number(item.telegram_message_id) === Number(message.reply_to_telegram_message_id));
    const hasReply = replyId != null || message.reply_to_telegram_message_id != null;
    const quoteText = quoted?.text || quoted?.caption ||
      (quoted?.attachment ? "Вложение" : "Ответ на сообщение");
    const quote = !hasReply ? "" : quoted
      ? '<button type="button" class="inbox-message-reply" data-inbox-jump="' +
        text(quoted.id) + '">' + text(quoteText.slice(0, 110)) + '</button>'
      : '<span class="inbox-message-reply">' + text(quoteText) + '</span>';
    return (isUnread ? '<div class="inbox-unread-divider">Непрочитанные сообщения</div>' : "") +
      '<article class="inbox-message ' + (outgoing ? "is-outgoing" : "is-incoming") +
      (delivery === "failed" ? ' has-failure' : "") + '" data-inbox-message="' + text(message.id) +
      '" tabindex="0">' +
      (showAuthor ? '<span class="inbox-message-author" title="Telegram ID: ' + text(authorId) +
        '">' + text(inbox.authorLabels.get(String(authorId))) + '</span>' : "") +
      quote +
      mediaMarkup(message) +
      (body ? '<span class="inbox-message-body">' + text(body) + '</span>' : "") +
      '<span class="inbox-message-meta"><time>' + text(messageClock(message.created_at)) + '</time>' +
      (delivery ? '<span class="inbox-delivery' + (delivery === "failed" ? ' inbox-failed' : "") +
        '">' + text(statusText[delivery] || delivery) + '</span>' : "") +
      '</span>' +
      '<span class="inbox-message-actions">' +
      '<button type="button" data-inbox-reply="' + text(message.id) +
        '" aria-label="Ответить на сообщение" title="Ответить">↶</button>' +
      (delivery === "failed" ? '<button type="button" class="inbox-retry-action" data-inbox-retry="' +
        text(message.id) + '">' + (message._sendRequest ? "Повторить" : "Отправить заново") + '</button>' : "") +
      '</span></article>';
  }).join("");
  el("inbox-older")?.classList.toggle("hidden", !inbox.historyCursor);
  if (el("inbox-older")) el("inbox-older").disabled = inbox.historyLoading;
  status("inbox-history-status", inbox.historyLoading
    ? "Загружаем сообщения…" : inbox.historyError || (inbox.messages.length ? "" : "Сообщений пока нет."));
  renderReply();
  syncMedia();
  if (preserveScroll) scroll.scrollTop = oldTop + scroll.scrollHeight - oldHeight;
  else if (atBottom || !oldHeight) scroll.scrollTop = scroll.scrollHeight;
  else scroll.scrollTop = oldTop;
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
    const page = response.items || [];
    const continuous = !refresh || page.some((item) =>
      inbox.messages.some((existing) => existing.id === item.id));
    const preserved = older || (refresh && continuous) ? inbox.messages : [];
    const byId = new Map(preserved.map((item) => [item.id, item]));
    for (const item of page) byId.set(item.id, item);
    inbox.messages = [...byId.values()].sort((a, b) => Number(a.id) - Number(b.id));
    if (refresh && !continuous) {
      inbox.seenIncoming.clear();
      inbox.current.unread_count = null;
    }
    if (older || !refresh || !continuous) inbox.historyCursor = response.next_cursor ?? null;
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

function visibleIncomingIds() {
  const history = el("inbox-history");
  if (!isOpen() || el("inbox-history-screen")?.hidden ||
      document.visibilityState !== "visible" || !document.hasFocus() || !history) return [];
  const viewport = history.getBoundingClientRect();
  if (!viewport.width || !viewport.height) return [];
  const visible = [];
  for (const message of history.querySelectorAll(".inbox-message.is-incoming")) {
    const rect = message.getBoundingClientRect();
    const visibleHeight = Math.min(rect.bottom, viewport.bottom) - Math.max(rect.top, viewport.top);
    if (visibleHeight >= Math.min(24, rect.height / 2)) {
      const id = Number(message.dataset.inboxMessage);
      if (Number.isInteger(id) && id > 0) visible.push(id);
    }
  }
  return visible;
}

async function markRead() {
  if (!inbox.client || !inbox.current) return;
  const visible = visibleIncomingIds();
  if (!visible.length) return;
  for (const id of visible) inbox.seenIncoming.add(id);
  const unreadCount = inbox.current.unread_count;
  if (!Number.isInteger(unreadCount) || unreadCount <= 0) return;
  const incoming = inbox.messages.filter((message) => message.direction === "incoming" &&
    Number.isInteger(message.id)).map((message) => message.id);
  // A monotone marker cannot skip any unread message, even one on an unloaded page.
  if (incoming.length < unreadCount) return;
  let lastSeen = null;
  for (const id of incoming.slice(-unreadCount)) {
    if (!inbox.seenIncoming.has(id)) break;
    lastSeen = id;
  }
  if (!lastSeen || lastSeen <= Number(inbox.readThrough || 0) ||
      lastSeen <= Number(inbox.readingId || 0)) return;
  const client = inbox.client;
  const id = inbox.current.id;
  const controller = new AbortController();
  inbox.readController?.abort();
  inbox.readController = controller;
  inbox.readingId = lastSeen;
  try {
    const result = await client.read(id, lastSeen, { signal: controller.signal });
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
    if (inbox.readingId === lastSeen) inbox.readingId = null;
  }
}

async function openConversation(conversation) {
  ++inbox.newRequest;
  inbox.newController?.abort();
  clearTimeout(inbox.newSearchTimer);
  ++inbox.openRequest;
  inbox.openController?.abort();
  inbox.openingUser = false;
  inbox.historyController?.abort();
  inbox.readController?.abort();
  inbox.current = conversation;
  clearMedia();
  inbox.messages = [];
  inbox.seenIncoming.clear();
  inbox.authorLabels.clear();
  inbox.draftFile = null;
  inbox.draftPending = null;
  el("inbox-file").value = "";
  renderFilePreview();
  inbox.historyCursor = null;
  inbox.readThrough = null;
  inbox.readingId = null;
  inbox.historyError = "";
  inbox.replyTo = null;
  ++inbox.historyRequest;
  el("inbox-list-screen").hidden = true;
  el("inbox-new-screen").hidden = true;
  el("inbox-history-screen").hidden = false;
  el("inbox-new").hidden = true;
  el("inbox-back").hidden = false;
  el("inbox-title").textContent = label(conversation.user);
  el("inbox-text").value = "";
  resizeComposer();
  renderHistory();
  el("inbox-text").focus();
  await loadMessages();
}

async function openForUser(userId, user) {
  if (!inbox.client || inbox.openingUser) return;
  inbox.openingUser = true;
  const generation = ++inbox.openRequest;
  inbox.openController?.abort();
  const controller = new AbortController();
  inbox.openController = controller;
  const client = inbox.client;
  try {
    const result = await client.open(Number(userId), { signal: controller.signal });
    if (generation !== inbox.openRequest || controller.signal.aborted || client !== inbox.client || !isOpen()) return;
    const existing = inbox.conversations.find((item) => Number(item.id) === Number(result.conversation_id));
    await openConversation(existing || { id: result.conversation_id,
      user: { id: userId, full_name: user?.full_name, study_group_name: user?.study_group?.code },
      unread_count: 0 });
  } catch (error) {
    if (!isAbort(error) && generation === inbox.openRequest && client === inbox.client &&
        !handleRequestError(error))
      status("inbox-new-status", error?.status === 404
        ? "Пользователь ещё не завершил регистрацию."
        : error?.message || "Не удалось открыть диалог.");
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
    const { message } = pending._sendRequest.file
      ? await client.upload(id, pending._sendRequest)
      : await client.send(id, pending._sendRequest);
    if (client !== inbox.client || inbox.current?.id !== id) return;
    if (pending._sendRequest.file && inbox.draftPending === pending) {
      const { file, caption } = pending._sendRequest;
      if (inbox.draftFile?.file === file && el("inbox-text").value === caption) {
        inbox.draftFile = null;
        inbox.draftPending = null;
        el("inbox-file").value = "";
        el("inbox-text").value = "";
        inbox.replyTo = null;
        resizeComposer();
        renderFilePreview();
      } else {
        inbox.draftPending = null;
      }
    }
    inbox.messages = inbox.messages.filter((item) => item !== pending && item.id !== message.id);
    inbox.messages.push({ ...message, _replyTo: pending._replyTo });
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

function resizeComposer() {
  const field = el("inbox-text");
  if (!field) return;
  field.style.height = "44px";
  field.style.height = Math.min(140, Math.max(44, field.scrollHeight)) + "px";
}

function normalizedFile(file) {
  if (file.type) return file;
  const ext = file.name.split(".").at(-1)?.toLowerCase();
  const mimes = {
    jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png",
    pdf: "application/pdf", zip: "application/zip", txt: "text/plain",
    docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ogg: "audio/ogg", oga: "audio/ogg",
  };
  return mimes[ext] ? new File([file], file.name, { type: mimes[ext] }) : file;
}

function chooseAttachment(file) {
  inbox.draftFile = file ? { file: normalizedFile(file), kind: fileKind(normalizedFile(file)) } : null;
  inbox.draftPending = null;
  renderFilePreview();
  status("inbox-file-status", "");
  if (!inbox.draftFile) return;
  if (inbox.draftFile.file.size > MAX_UPLOAD_BYTES) {
    status("inbox-file-status", "Файл превышает 20 МБ. Выберите файл меньшего размера.");
  } else if (!FILE_TYPES.has(inbox.draftFile.file.type)) {
    status("inbox-file-status", "Формат файла не поддерживается.");
  }
}

function sendAttachment(caption, replyTo = null) {
  if (!inbox.client || !inbox.current || !inbox.draftFile || inbox.sending) return;
  const { file, kind } = inbox.draftFile;
  if (file.size > MAX_UPLOAD_BYTES) {
    status("inbox-file-status", "Файл превышает 20 МБ. Выберите файл меньшего размера.");
    return;
  }
  if (!FILE_TYPES.has(file.type)) {
    status("inbox-file-status", "Формат файла не поддерживается.");
    return;
  }
  if (caption.length > 1024) {
    status("inbox-file-status", "Подпись не должна превышать 1024 символа.");
    return;
  }
  const old = inbox.draftPending;
  if (old && old._sendRequest?.file === file &&
      old._sendRequest.caption === caption &&
      old._sendRequest.reply_to_message_id === replyTo) {
    void deliverPending(old);
    return;
  }
  const request = Object.freeze({
    file, kind, caption, reply_to_message_id: replyTo, idempotency_key: key(),
  });
  const pending = {
    id: "pending-" + request.idempotency_key,
    conversation_id: inbox.current.id,
    direction: "outgoing",
    kind,
    text: null,
    caption,
    created_at: new Date().toISOString(),
    author_admin_telegram_id: null,
    telegram_message_id: null,
    reply_to_telegram_message_id: null,
    delivery_status: "queued",
    delivery_error_code: null,
    attachment: { kind, filename: file.name, size: file.size, mime_type: file.type, available: false },
    _replyTo: replyTo,
    _sendRequest: request,
  };
  inbox.messages.push(pending);
  inbox.draftPending = pending;
  status("inbox-file-status", "");
  renderHistory();
  void deliverPending(pending);
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
  resizeComposer();
  inbox.replyTo = null;
  renderHistory();
  void deliverPending(pending);
}

function openInbox() {
  if (!inbox.client || isOpen()) return;
  inbox.opener = document.activeElement;
  dialog().showModal();
  const back = telegramWebApp()?.BackButton;
  inbox.backHandler = navigateBack;
  back?.onClick?.(inbox.backHandler);
  back?.show?.();
  renderGroups();
  showList();
}

function navigateBack() {
  if (!el("inbox-list-screen")?.hidden) closeInbox();
  else showList();
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
  clearTimeout(inbox.newSearchTimer);
  const back = telegramWebApp()?.BackButton;
  if (inbox.backHandler) back?.offClick?.(inbox.backHandler);
  inbox.backHandler = null;
  back?.hide?.();
  if (inbox.opener?.isConnected && !inbox.opener.closest(".hidden, [hidden]"))
    inbox.opener.focus();
  inbox.opener = null;
  inbox.readingId = null;
  clearMedia();
}

export function configureInboxClient(client) {
  if (client !== null && (!client || ["summary", "conversations", "users", "open", "messages", "send", "upload", "download", "read"].some((method) => typeof client[method] !== "function")))
    throw new TypeError("Inbox client does not implement contract #24");
  if (isOpen()) closeInbox();
  inbox.client = client;
  inbox.unread = 0;
  if (!client) {
    cancelRequests();
    ++inbox.summaryRequest;
    inbox.conversations = [];
    inbox.messages = [];
    inbox.draftFile = null;
    inbox.draftPending = null;
    inbox.newUsers = [];
    inbox.seenIncoming.clear();
    inbox.authorLabels.clear();
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
  if (client) void loadSummary();
}

export function bindInbox() {
  if (bound) return;
  bound = true;
  el("inbox-open")?.addEventListener("click", openInbox);
  el("inbox-close")?.addEventListener("click", closeInbox);
  el("inbox-back")?.addEventListener("click", showList);
  el("inbox-new")?.addEventListener("click", showNewDialog);
  el("inbox-new-search")?.addEventListener("input", (event) => {
    inbox.newQuery = event.target.value;
    ++inbox.newRequest;
    clearTimeout(inbox.newSearchTimer);
    inbox.newController?.abort();
    inbox.newSearchTimer = window.setTimeout(() => void loadNewUsers(true), SEARCH_DEBOUNCE_MS);
  });
  el("inbox-new-group")?.addEventListener("change", (event) => {
    inbox.newGroup = event.target.value;
    ++inbox.newRequest;
    clearTimeout(inbox.newSearchTimer);
    void loadNewUsers(true);
  });
  el("inbox-new-more")?.addEventListener("click", () => void loadNewUsers());
  el("inbox-new-retry")?.addEventListener("click", () => void loadNewUsers(true));
  el("inbox-new-users")?.addEventListener("click", (event) => {
    const row = event.target.closest("[data-inbox-new-user]");
    if (!row) return;
    const user = inbox.newUsers.find((item) => Number(item.id) === Number(row.dataset.inboxNewUser));
    if (user) void openForUser(user.id, user);
  });
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
  el("inbox-conversations")?.addEventListener("click", (event) => {
    const row = event.target.closest("[data-inbox-conversation]");
    if (!row) return;
    const item = inbox.conversations.find((conversation) => conversation.id === Number(row.dataset.inboxConversation));
    if (item) void openConversation(item);
  });
  el("inbox-history")?.addEventListener("click", (event) => {
    const reply = event.target.closest("[data-inbox-reply]");
    const retry = event.target.closest("[data-inbox-retry]");
    const jump = event.target.closest("[data-inbox-jump]");
    if (jump) {
      const target = el("inbox-messages")?.querySelector(
        '[data-inbox-message="' + Number(jump.dataset.inboxJump) + '"]');
      target?.scrollIntoView({ block: "center", behavior: "smooth" });
      target?.focus({ preventScroll: true });
    }
    const photo = event.target.closest("[data-inbox-view-photo]");
    const voice = event.target.closest("[data-inbox-load-voice]");
    const download = event.target.closest("[data-inbox-download]");
    const reload = event.target.closest("[data-inbox-reload-media]");
    const mediaTarget = photo || voice || download || reload;
    if (mediaTarget) {
      const messageId = Number(photo?.dataset.inboxViewPhoto || voice?.dataset.inboxLoadVoice ||
        download?.dataset.inboxDownload || reload?.dataset.inboxReloadMedia);
      const message = inbox.messages.find((item) => Number(item.id) === messageId);
      if (message) {
        if (download) void loadMedia(message, { download: true });
        else if (voice) void loadMedia(message);
        else if (reload) void loadMedia(message, { download: message.kind === "document" });
        else if (photo) {
          const show = (url) => {
            if (!url || inbox.current?.id !== message.conversation_id || !isOpen()) return;
            el("inbox-photo-full").src = url;
            el("inbox-photo-viewer").showModal();
          };
          const url = inbox.mediaUrls.get(messageId);
          if (url) show(url);
          else void loadMedia(message).then(show);
        }
      }
    }
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
        else if (message.kind === "text" && message.text) sendMessage(message.text);
        else status("inbox-compose-status", "Для повторной отправки вложения выберите исходный файл.");
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
  el("inbox-text")?.addEventListener("input", resizeComposer);
  el("inbox-photo-close")?.addEventListener("click", () => el("inbox-photo-viewer")?.close());
  el("inbox-photo-viewer")?.addEventListener("click", (event) => {
    if (event.target.id === "inbox-photo-viewer") event.target.close();
  });
  el("inbox-file")?.addEventListener("change", (event) => {
    chooseAttachment(event.target.files?.[0] || null);
  });
  el("inbox-file-preview")?.addEventListener("click", (event) => {
    if (!event.target.closest("#inbox-remove-file")) return;
    inbox.draftFile = null;
    inbox.draftPending = null;
    el("inbox-file").value = "";
    renderFilePreview();
  });
  el("inbox-composer")?.addEventListener("submit", (event) => {
    event.preventDefault();
    status("inbox-compose-status", "");
    if (inbox.draftFile) sendAttachment(el("inbox-text").value, inbox.replyTo);
    else void sendMessage(el("inbox-text").value, inbox.replyTo);
  });
}

export function refreshInboxFromEvent(conversationId = null, { reconnect = false } = {}) {
  if (!inbox.client) return;
  void loadSummary();
  if (!isOpen()) return;
  if (!el("inbox-list-screen")?.hidden || inbox.current) void loadConversations(true);
  if (el("inbox-list-screen")?.hidden && inbox.current &&
      (reconnect || conversationId == null || Number(inbox.current.id) === Number(conversationId))) {
    void loadMessages({ refresh: true });
  }
}
