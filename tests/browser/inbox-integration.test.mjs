import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";

const root = resolve(fileURLToPath(new URL("../..", import.meta.url)), "frontend");
const screenshots = process.env.BROWSER_EVIDENCE_DIR || resolve(tmpdir(), "topsbottg-browser-evidence");
const mime = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript" };

const incoming = (id, conversation_id = 11) => ({
  id, conversation_id, direction: "incoming", kind: "text",
  text: "Входящее " + id, caption: null, created_at: "2026-11-15T13:00:00Z",
  author_admin_telegram_id: null, telegram_message_id: id + 100,
  reply_to_telegram_message_id: null, delivery_status: null,
  delivery_error_code: null, attachment: null,
});

async function withBackend(run) {
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, "http://localhost").pathname;
    const file = resolve(root, path.replace(/^\/miniapp\/?/, "") || "index.html");
    if (file !== resolve(root, "index.html") && !file.startsWith(root + sep))
      return res.writeHead(403).end();
    try {
      res.writeHead(200, { "Content-Type": mime[extname(file)] || "application/octet-stream" });
      res.end(await readFile(file));
    } catch { res.writeHead(404).end("Not found"); }
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  const browser = await chromium.launch({ headless: true });
  const db = {
    messages: [incoming(201), incoming(202)],
    users: [
      { id: 1, full_name: "Анна Петрова", study_group: { id: 9, code: "Г-9" } },
      { id: 2, full_name: "Иван Иванов", study_group: null },
      { id: 3, full_name: "Новый Пользователь", study_group: null },
    ],
    conversations: [{ id: 11, user_id: 1 }, { id: 12, user_id: 2 }],
    seen: new Map([[123, new Map()], [555, new Map()]]),
    granted: new Set([123, 555]),
    sendByKey: new Map(),
    requests: [],
    failSend: false,
    failList: false,
    nextMessageId: 300,
    nextConversationId: 13,
    lastKey: null,
    delayedQuery: null,
    delayedOpenUserId: null,
    releaseOpen: null,
  };

  const json = (route, payload, status = 200) => route.fulfill({
    status, contentType: "application/json", body: JSON.stringify(payload),
  });
  const messagesFor = (id) => db.messages.filter((message) => message.conversation_id === id);
  const unreadFor = (admin, id) =>
    messagesFor(id).filter((message) => message.direction === "incoming" &&
      message.id > (db.seen.get(admin)?.get(id) || 0)).length;
  const userOut = (id) => {
    const user = db.users.find((item) => item.id === id);
    return { id, telegram_id: id + 90000, full_name: user.full_name,
      study_group_id: user.study_group?.id ?? null,
      study_group_name: user.study_group?.code ?? null };
  };
  const conversationOut = (conv, admin) => {
    const latest = messagesFor(conv.id).at(-1) || null;
    return {
      id: conv.id, user: userOut(conv.user_id), last_message: latest,
      last_message_at: latest?.created_at || null, unread_count: unreadFor(admin, conv.id),
    };
  };

  async function createPage(admin = 123, width = 390) {
    const page = await browser.newPage({ viewport: { width, height: 844 } });
    page.setDefaultTimeout(12000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript((id) => {
      const callbacks = {};
      const backHandlers = new Set();
      window.Telegram = { WebApp: {
        initData: "admin:" + id, colorScheme: "light", themeParams: {}, viewportHeight: innerHeight,
        ready() {}, expand() {},
        onEvent(name, fn) { (callbacks[name] ??= []).push(fn); },
        __callbacks: callbacks,
        BackButton: {
          onClick(handler) { backHandlers.add(handler); },
          offClick(handler) { backHandlers.delete(handler); },
          show() {}, hide() {},
        },
      } };
      window.__sse = [];
      class MockEventSource {
        static CLOSED = 2;
        static OPEN = 1;
        constructor() {
          this.readyState = 1;
          this.listeners = new Map();
          window.__sse.push(this);
          setTimeout(() => this.emit("open", {}), 0);
        }
        addEventListener(event, handler) {
          const list = this.listeners.get(event) || [];
          list.push(handler);
          this.listeners.set(event, list);
        }
        emit(event, payload) {
          for (const handler of this.listeners.get(event) || []) {
            handler({ data: JSON.stringify(payload) });
          }
        }
        close() { this.readyState = 2; }
      }
      window.EventSource = MockEventSource;
    }, admin);
    await page.route("https://telegram.org/**", (route) => route.abort());
    await page.route("**/api/**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname;
      const method = req.method();
      const identity = req.headers()["x-telegram-init-data"] || "";
      const actor = Number(identity.replace("admin:", ""));
      db.requests.push({ method, path, query: url.search, actor,
        key: req.headers()["idempotency-key"] || null });
      if (path === "/api/admin/events") return json(route, { detail: "SSE mocked separately" }, 501);
      if (!db.granted.has(actor) && path !== "/api/admin/me") return json(route, { detail: "admin only" }, 403);
      if (path === "/api/admin/me") {
        return db.granted.has(actor)
          ? json(route, { telegram_user_id: actor, role: actor === 123 ? "owner" : "admin" })
          : json(route, { detail: "admin only" }, 403);
      }
      if (path === "/api/admin/events-token") return json(route, { expires_in: 60 });
      if (path === "/api/admin/users") {
        const search = (url.searchParams.get("search") || "").toLowerCase();
        const group = url.searchParams.get("study_group_id");
        const offset = Number(url.searchParams.get("offset") || 0);
        const limit = Number(url.searchParams.get("limit") || 50);
        const matches = db.users.filter((user) =>
          user.full_name.toLowerCase().includes(search) &&
          (!group || String(user.study_group?.id) === group));
        return json(route, {
          items: matches.slice(offset, offset + limit), offset,
          has_more: offset + limit < matches.length,
        });
      }
      if (path === "/api/admin/study-groups") return json(route, [{ id: 9, code: "Г-9" }]);
      if (path === "/api/admin/payouts" ||
          path === "/api/admin/admin-directory" ||
          path === "/api/admin/admin-invitations") return json(route, []);
      if (path === "/api/admin/conversations/summary") {
        return json(route, { unread_count: db.conversations.reduce((sum, conv) =>
          sum + unreadFor(actor, conv.id), 0) });
      }
      if (path === "/api/admin/conversations" && method === "GET") {
        if (db.failList) return json(route, { detail: "Список временно недоступен" }, 503);
        const query = url.searchParams.get("query") || "";
        if (query === db.delayedQuery) await new Promise((done) => setTimeout(done, 800));
        const group = url.searchParams.get("study_group_id");
        const cursor = url.searchParams.get("cursor");
        const limit = Math.min(50, Number(url.searchParams.get("limit") || 50));
        const items = db.conversations.filter((conv) => {
          const user = userOut(conv.user_id);
          return user.full_name.toLowerCase().includes(query.toLowerCase()) &&
            (!group || String(user.study_group_id) === group);
        });
        const offset = cursor ? Number(cursor.replace("offset:", "")) : 0;
        return json(route, { items: items.slice(offset, offset + limit).map((conv) =>
          conversationOut(conv, actor)),
        next_cursor: offset + limit < items.length ? "offset:" + (offset + limit) : null });
      }
      if (path === "/api/admin/conversations" && method === "POST") {
        const { user_id } = JSON.parse(req.postData() || "{}");
        if (db.delayedOpenUserId === user_id)
          await new Promise((resolve) => { db.releaseOpen = resolve; });
        if (!db.users.find((u) => u.id === user_id)) return json(route, { detail: "not found" }, 404);
        let conv = db.conversations.find((c) => c.user_id === user_id);
        if (!conv) { conv = { id: db.nextConversationId++, user_id }; db.conversations.push(conv); }
        return json(route, { conversation_id: conv.id });
      }
      const match = /^\/api\/admin\/conversations\/(\d+)\/(messages|read)$/.exec(path);
      if (match) {
        const cid = Number(match[1]), action = match[2];
        if (!db.conversations.some((conv) => conv.id === cid)) return json(route, { detail: "not found" }, 404);
        if (action === "messages" && method === "GET") {
          const before = url.searchParams.get("before");
          const limit = Number(url.searchParams.get("limit") || 50);
          const all = messagesFor(cid).filter((message) => before === null ||
            message.id < Number(before)).sort((a, b) => a.id - b.id);
          const page = all.slice(-limit);
          return json(route, { items: page,
            next_cursor: all.length > page.length ? String(page[0].id) : null });
        }
        if (action === "messages" && method === "POST") {
          const body = JSON.parse(req.postData());
          const key = req.headers()["idempotency-key"];
          db.lastKey = key;
          const existing = db.sendByKey.get(key);
          if (existing && existing.text !== body.text) return json(route, { detail: "conflict" }, 409);
          const message = existing || {
            id: db.nextMessageId++, conversation_id: cid, direction: "outgoing",
            kind: "text", text: body.text, caption: null,
            created_at: "2026-11-15T13:01:00Z", author_admin_telegram_id: actor,
            telegram_message_id: null, reply_to_telegram_message_id: null,
            delivery_status: "queued", delivery_error_code: null, attachment: null,
          };
          if (!existing) { db.sendByKey.set(key, message); db.messages.push(message); }
          if (db.failSend) { db.failSend = false; return route.abort("failed"); }
          return json(route, { message }, 202);
        }
        if (action === "read" && method === "POST") {
          const body = JSON.parse(req.postData());
          const previous = db.seen.get(actor).get(cid) || 0;
          const newId = Math.max(previous, body.last_read_message_id);
          db.seen.get(actor).set(cid, newId);
          return json(route, { last_read_message_id: newId, unread_count: unreadFor(actor, cid) });
        }
      }
      return json(route, { detail: "unexpected endpoint" }, 501);
    });
    await page.goto("http://127.0.0.1:" + server.address().port + "/miniapp/");
    await page.locator("#inbox-open").waitFor({ state: db.granted.has(admin) ? "visible" : "hidden" });
    if (db.granted.has(admin)) await page.waitForFunction(() => document.querySelector("#inbox-open:not(.hidden)") != null);
    return { page, errors, admin };
  }

  try {
    await run({ db, createPage, browser, unreadFor });
  } finally {
    await browser.close();
    if (server.listening) await new Promise((done) => server.close(done));
  }
}

test("HTTP inbox, SSE delivery, independent admin read markers, reload and navigation", async () => {
  await withBackend(async ({ db, createPage, unreadFor }) => {
    const owner = await createPage(123, 390);
    const admin = await createPage(555, 1280);
    const a = owner.page, b = admin.page;
    assert.equal(await a.locator("#inbox-unread").innerText(), "2");
    assert.equal(await b.locator("#inbox-unread").innerText(), "2");
    assert.deepEqual((await a.locator("[data-mobile-view]").allTextContents()).map((s) => s.trim()),
      ["Создать", "Выбор", "Получатели", "История"]);
    await a.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      state.selectedPayoutId = 71;
      state.selectedUsers = new Set([1, 2]);
      state.selectedUsersByPayout.set(71, new Set([1, 2]));
    });
    await a.locator("#inbox-open").click();
    await a.locator("#inbox-conversations").getByText("Анна Петрова").click();
    await a.locator("#inbox-history [data-inbox-message='202']").waitFor();
    await a.waitForFunction(() => document.querySelector("#inbox-unread").textContent === "0");
    assert.equal(unreadFor(123, 11), 0);
    assert.equal(unreadFor(555, 11), 2);
    assert.equal(await b.locator("#inbox-unread").innerText(), "2");

    await a.locator("#inbox-text").fill("Ответ через outbox");
    await a.locator("#inbox-send").click();
    await a.locator("#inbox-history").getByText("Ответ через outbox").waitFor();
    await a.locator("#inbox-history").getByText("В очереди").waitFor();
    assert.ok(db.lastKey && db.lastKey.length > 10);
    assert.equal(db.sendByKey.size, 1);
    assert.equal(db.requests.filter((entry) =>
      entry.path.endsWith("/messages") && entry.method === "POST").length, 1);

    db.sendByKey.get(db.lastKey).delivery_status = "sent";
    db.sendByKey.get(db.lastKey).telegram_message_id = 701;
    db.messages.push(incoming(203));
    await Promise.all([a,b].map((page) => page.evaluate(() =>
      window.__sse.at(-1).emit("chat.changed", { conversation_id: 11 }))));
    await a.locator("#inbox-history").getByText("Отправлено").waitFor();
    await b.waitForFunction(() => document.querySelector("#inbox-unread").textContent === "3");
    await a.locator("#inbox-history [data-inbox-message='203']").waitFor();
    assert.equal(await b.locator("#inbox-unread").innerText(), "3");

    await a.evaluate(() => window.__sse.at(-1).onerror());
    await a.waitForFunction(() => window.__sse.length >= 2);
    await a.evaluate(() => window.__sse.at(-1).emit("open", {}));
    await a.locator("#inbox-history [data-inbox-message='203']").waitFor();
    assert.deepEqual(await a.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      return [state.selectedPayoutId, [...state.selectedUsers], [...state.selectedUsersByPayout.get(71)]];
    }), [71, [1, 2], [1, 2]]);
    await a.locator("#inbox-close").click();
    for (const view of ["composer", "users", "recipients", "payouts"]) {
      await a.locator('[data-mobile-view="' + view + '"]').click();
      assert.equal(await a.locator('[data-screen="' + view + '"]').isVisible(), true);
    }

    await a.reload();
    await a.locator("#inbox-open").waitFor({ state: "visible" });
    await a.locator("#inbox-open").click();
    await a.locator("#inbox-conversations").getByText("Анна Петрова").click();
    await a.locator("#inbox-history [data-inbox-message='300']").waitFor();
    assert.equal(db.sendByKey.size, 1);
    await mkdir(screenshots, { recursive: true });
    await a.screenshot({ path: resolve(screenshots, "inbox-api-mobile.png") });
    await b.locator("#inbox-open").click();
    await b.screenshot({ path: resolve(screenshots, "inbox-api-desktop.png") });
    assert.deepEqual(owner.errors, []);
    assert.deepEqual(admin.errors, []);
  });
});

test("HTTP inbox: stale search, cursor, retry after lost 202, authorization revocation", async () => {
  await withBackend(async ({ db, createPage }) => {
    const { page, errors } = await createPage(555);
    await page.locator("#inbox-open").click();
    db.delayedQuery = "Анна";
    await page.locator("#inbox-search").fill("Анна");
    await page.waitForTimeout(340);
    await page.locator("#inbox-search").fill("Иван");
    await page.locator("#inbox-conversations").getByText("Иван Иванов").waitFor();
    await page.waitForTimeout(900);
    assert.equal(await page.locator("#inbox-conversations").getByText("Анна Петрова").count(), 0);

    await page.locator("#inbox-search").fill("");
    await page.locator("#inbox-conversations").getByText("Анна Петрова").waitFor();
    await page.locator("#inbox-conversations").getByText("Анна Петрова").click();
    await page.locator("#inbox-history [data-inbox-message='202']").waitFor();
    db.failSend = true;
    await page.locator("#inbox-text").fill("Повтор с тем же ключом");
    await page.locator("#inbox-send").click();
    await page.locator("#inbox-history").getByText("Повтор с тем же ключом").waitFor();
    await page.locator("#inbox-history").getByText("Ошибка отправки").waitFor();
    await page.locator("#inbox-history .inbox-message").filter({ hasText: "Повтор с тем же ключом" })
      .locator("[data-inbox-retry]").click();
    await page.locator("#inbox-history .inbox-message").filter({ hasText: "Повтор с тем же ключом" })
      .filter({ hasText: "В очереди" }).waitFor();
    const attempts = db.requests.filter((req) =>
      req.method === "POST" && req.path === "/api/admin/conversations/11/messages");
    assert.equal(attempts.length, 2);
    assert.equal(attempts[0].key, attempts[1].key);
    assert.equal(db.sendByKey.size, 1);
    assert.equal(await page.locator("#inbox-history .inbox-message")
      .filter({ hasText: "Повтор с тем же ключом" }).count(), 1);

    db.granted.delete(555);
    await page.evaluate(() => window.__sse.at(-1).emit("chat.changed", { conversation_id: 11 }));
    await page.locator("#inbox-open").waitFor({ state: "hidden" });
    assert.equal(await page.locator("#inbox-dialog").isVisible(), false);
    assert.equal(await page.locator("#inbox-history .inbox-message").count(), 0);
    assert.ok(db.requests.some((req) =>
      req.actor === 555 && req.path.includes("/conversations") && req.method === "GET"));
    assert.deepEqual(errors, []);
  });
});


test("More than 50 unread messages require continuous actual viewing; other admin is independent", async () => {
  await withBackend(async ({ db, createPage, unreadFor }) => {
    db.messages = Array.from({ length: 120 }, (_, index) => incoming(index + 1));
    const owner = await createPage(123, 390);
    const second = await createPage(555, 1280);
    const a = owner.page;
    await a.locator("#inbox-open").click();
    await a.locator("#inbox-conversations").getByText("Анна Петрова").click();
    await a.locator("#inbox-history [data-inbox-message='120']").waitFor();
    assert.equal(await a.locator("#inbox-history .is-incoming").count(), 50);
    assert.equal(unreadFor(123, 11), 120);
    assert.equal(unreadFor(555, 11), 120);
    assert.equal(db.requests.filter((r) => r.path === "/api/admin/conversations/11/read").length, 0);

    await a.locator("#inbox-older").click();
    await a.locator("#inbox-history [data-inbox-message='21']").waitFor();
    assert.equal(unreadFor(123, 11), 120);
    await a.locator("#inbox-older").click();
    await a.locator("#inbox-history [data-inbox-message='1']").waitFor();
    assert.equal(await a.locator("#inbox-history .is-incoming").count(), 120);
    assert.equal(unreadFor(123, 11), 120,
      "Loading a page is not equivalent to viewing its messages");

    await a.locator("#inbox-history [data-inbox-message='1']").scrollIntoViewIfNeeded();
    await a.waitForFunction(() => document.querySelector("#inbox-history").scrollTop < 60);
    await a.waitForTimeout(150);
    assert.ok(unreadFor(123, 11) > 0, "Reading only the first page must leave later messages unread");
    assert.equal(unreadFor(555, 11), 120);

    await a.locator("#inbox-history [data-inbox-message='120']").scrollIntoViewIfNeeded();
    await a.waitForTimeout(150);
    assert.ok(unreadFor(123, 11) > 0,
      "Jumping to the newest message cannot skip the unviewed middle");
    assert.equal(unreadFor(555, 11), 120);

    await a.evaluate(async () => {
      const list = document.getElementById("inbox-history");
      const step = Math.max(40, list.clientHeight / 3);
      for (let top = 0; top <= list.scrollHeight + step; top += step) {
        list.scrollTop = top;
        list.dispatchEvent(new Event("scroll"));
        await new Promise((resolve) => setTimeout(resolve, 12));
      }
      list.scrollTop = list.scrollHeight;
      list.dispatchEvent(new Event("scroll"));
    });
    await a.waitForFunction(() => document.querySelector("#inbox-unread").textContent === "0");
    assert.equal(unreadFor(123, 11), 0);
    assert.equal(unreadFor(555, 11), 120);
    assert.equal(await second.page.locator("#inbox-unread").innerText(), "99+");
    assert.deepEqual(owner.errors, []);
    assert.deepEqual(second.errors, []);
  });
});

test("Late conversation creation cannot replace a manually selected chat and draft", async () => {
  await withBackend(async ({ db, createPage }) => {
    const { page, errors } = await createPage(123);
    db.delayedOpenUserId = 3;
    await page.locator('[data-mobile-view="users"]').click();
    assert.equal(await page.locator("[data-inbox-write]").count(), 0);
    await page.locator("#inbox-open").click();
    await page.locator("#inbox-new").click();
    await page.locator("#inbox-new-users [data-inbox-new-user='3']").click();
    await page.waitForFunction(() => window.document.querySelector("#inbox-new-screen:not([hidden])"));
    await page.locator("#inbox-back").click();
    await page.locator("#inbox-conversations").getByText("Анна Петрова").click();
    await page.locator("#inbox-history [data-inbox-message='202']").waitFor();
    await page.locator("#inbox-text").fill("Черновик выбранного диалога");
    db.releaseOpen?.();
    await page.waitForTimeout(350);
    assert.equal(await page.locator("#inbox-title").innerText(), "Анна Петрова");
    assert.equal(await page.locator("#inbox-text").inputValue(), "Черновик выбранного диалога");
    assert.equal(await page.locator("#inbox-dialog").isVisible(), true);
    assert.deepEqual(errors, []);
  });
});

test("SSE keeps reconnecting after multiple short failures and refetches missed messages", async () => {
  await withBackend(async ({ db, createPage }) => {
    const { page, errors } = await createPage(123);
    await page.locator("#inbox-open").click();
    await page.locator("#inbox-conversations").getByText("Анна Петрова").click();
    await page.waitForFunction(() => window.__sse?.length === 1);
    await page.evaluate(() => window.__sse.at(-1).onerror());
    await page.waitForFunction(() => window.__sse?.length === 2, null, { timeout: 5000 });
    await page.evaluate(() => window.__sse.at(-1).onerror());
    await page.waitForFunction(() => window.__sse?.length === 3, null, { timeout: 8000 });
    db.messages.push(incoming(203));
    await page.evaluate(() => window.__sse.at(-1).emit("open", {}));
    await page.locator("#inbox-history [data-inbox-message='203']").waitFor();
    assert.equal(await page.evaluate(() => window.__sse.at(-1).readyState), 1);
    assert.equal(db.requests.filter((r) => r.path === "/api/admin/events-token").length, 3);
    assert.deepEqual(errors, []);
  });
});


test("New dialog search is paginated and isolated from payout selection", async () => {
  await withBackend(async ({ db, createPage }) => {
    db.users.push(...Array.from({ length: 120 }, (_, index) => ({
      id: index + 10, full_name: "Участник " + String(index + 1).padStart(3, "0"),
      study_group: index % 2 === 0 ? { id: 9, code: "Г-9" } : null,
    })));
    const { page, errors } = await createPage(123, 390);
    await page.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      state.selectedPayoutId = 71;
      state.selectedUsers = new Set([1, 2]);
      state.selectedUsersByPayout.set(71, new Set([1, 2]));
    });
    await page.locator("#inbox-open").click();
    await page.locator("#inbox-new").click();
    await page.waitForFunction(() => document.querySelectorAll(".inbox-new-user").length === 50);
    await page.locator("#inbox-new-more").click();
    await page.waitForFunction(() => document.querySelectorAll(".inbox-new-user").length === 100);
    await page.locator("#inbox-new-more").click();
    await page.waitForFunction(() => document.querySelectorAll(".inbox-new-user").length === 123);
    assert.equal(await page.locator("#inbox-new-more").isVisible(), false);
    assert.ok(db.requests.some((request) => request.path === "/api/admin/users" &&
      request.query.includes("registered_only=true") &&
      request.query.includes("offset=100")));

    await page.locator("#inbox-new-group").selectOption("9");
    await page.waitForFunction(() => document.querySelectorAll(".inbox-new-user").length === 50);
    assert.ok(await page.locator("#inbox-new-users").getByText("Анна Петрова").count() === 1);
    await page.locator("#inbox-new-group").selectOption("");
    await page.locator("#inbox-new-search").fill("Новый Пользователь");
    await page.waitForFunction(() => document.querySelectorAll("#inbox-new-users .inbox-new-user").length === 1);
    assert.equal(await page.locator(".inbox-new-user").count(), 1);
    await page.locator("[data-inbox-new-user='3']").click();
    await page.locator("#inbox-title").getByText("Новый Пользователь").waitFor();
    await page.locator("#inbox-history-status").getByText("Сообщений пока нет.").waitFor();

    assert.deepEqual(await page.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      return [state.selectedPayoutId, [...state.selectedUsers], [...state.selectedUsersByPayout.get(71)]];
    }), [71, [1, 2], [1, 2]]);
    assert.equal(await page.locator("[data-inbox-write]").count(), 0);
    assert.equal(await page.locator("[data-mobile-view]").count(), 4);
    assert.deepEqual(errors, []);
  });
});
