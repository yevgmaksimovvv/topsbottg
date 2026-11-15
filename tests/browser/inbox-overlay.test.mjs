import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";

const root = resolve(fileURLToPath(new URL("../..", import.meta.url)), "frontend");
const evidence = process.env.BROWSER_EVIDENCE_DIR || resolve(tmpdir(), "topsbottg-browser-evidence");
const mime = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript" };

async function withPage(run) {
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, "http://localhost").pathname;
    const file = resolve(root, path.replace(/^\/miniapp\/?/, "") || "index.html");
    if (file !== resolve(root, "index.html") && !file.startsWith(root + sep))
      return res.writeHead(403).end();
    try {
      res.writeHead(200, { "Content-Type": mime[extname(file)] || "application/octet-stream" });
      res.end(await readFile(file));
    } catch {
      res.writeHead(404).end("Not found");
    }
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    page.setDefaultTimeout(10000);
    const errors = [];
    const requests = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(() => {
      const callbacks = {};
      const handlers = new Set();
      window.Telegram = {
        WebApp: {
          initData: "fixture", colorScheme: "light", themeParams: {},
          viewportHeight: innerHeight,
          ready() {}, expand() {},
          onEvent(name, listener) { (callbacks[name] ??= []).push(listener); },
          __callbacks: callbacks,
          BackButton: {
            onClick(handler) { handlers.add(handler); },
            offClick(handler) { handlers.delete(handler); },
            show() {}, hide() {},
            fire() { for (const handler of [...handlers]) handler(); },
            get count() { return handlers.size; },
          },
        },
      };
    });
    await page.route("https://telegram.org/**", (route) => route.abort());
    await page.route("**/api/**", (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      requests.push(request.method() + " " + path);
      let data = null;
      if (path === "/api/admin/me") data = { telegram_user_id: 123, role: "owner", is_admin: true };
      else if (path === "/api/admin/users")
        data = { items: [
          { id: 1, full_name: "Анна Петрова", study_group: { id: 9, code: "Г-9" } },
          { id: 2, full_name: "Иван Иванов", study_group: null },
          { id: 3, full_name: "Новый Пользователь", study_group: null },
        ], has_more: false, offset: 0 };
      else if (path === "/api/admin/study-groups")
        data = [{ id: 9, code: "Г-9" }];
      else if (path === "/api/admin/payouts" || path === "/api/admin/admin-directory" || path === "/api/admin/admin-invitations")
        data = [];
      else if (path === "/api/admin/conversations/summary") data = { unread_count: 0 };
      else if (path === "/api/admin/events-token")
        data = { token: "fixture", expires_in: 60 };
      else if (path === "/api/admin/events")
        return route.fulfill({ status: 200, contentType: "text/event-stream", body: ":connected\n\n" });
      return route.fulfill({
        status: data === null ? 501 : 200,
        contentType: "application/json",
        body: JSON.stringify(data === null ? { detail: "Unexpected endpoint" } : data),
      });
    });
    await page.goto("http://127.0.0.1:" + server.address().port + "/miniapp/");
    await page.waitForFunction(async () => (await import("/miniapp/js/store.js")).state.adminRole === "owner");
    await run({ page, requests });
    assert.deepEqual(errors, []);
    assert.equal(requests.some((request) => /\/conversations\/\d+\/messages/.test(request)), false,
      "Fixture must not send real conversation messages");
  } finally {
    await browser.close();
    if (server.listening) await new Promise((done) => server.close(done));
  }
}

async function installFixture(page, unread = 3) {
  await page.evaluate(async (unreadCount) => {
    const { configureInboxClient } = await import("/miniapp/js/inbox.js");
    const date = "2026-11-15T12:00:00Z";
    const messages = [
      { id: 51, conversation_id: 11, direction: "incoming", kind: "text",
        text: "Длинное сообщение " + "ОченьДлинныйТекстБезПробелов".repeat(150),
        caption: null, created_at: date, author_admin_telegram_id: null,
        telegram_message_id: 880, reply_to_telegram_message_id: null,
        delivery_status: null, delivery_error_code: null, attachment: null },
      { id: 52, conversation_id: 11, direction: "outgoing", kind: "text",
        text: "В очереди", caption: null, created_at: date, author_admin_telegram_id: 123,
        telegram_message_id: null, reply_to_telegram_message_id: 880,
        delivery_status: "queued", delivery_error_code: null, attachment: null },
      { id: 53, conversation_id: 11, direction: "outgoing", kind: "text",
        text: "В процессе", caption: null, created_at: date, author_admin_telegram_id: 456,
        telegram_message_id: null, reply_to_telegram_message_id: null,
        delivery_status: "sending", delivery_error_code: null, attachment: null },
      { id: 54, conversation_id: 11, direction: "outgoing", kind: "text",
        text: "Ошибка доставки", caption: null, created_at: date, author_admin_telegram_id: 123,
        telegram_message_id: null, reply_to_telegram_message_id: null,
        delivery_status: "failed", delivery_error_code: "blocked", attachment: null },
    ];
    const conversations = [
      { id: 11, user: { id: 1, full_name: "Анна Петрова", study_group_id: 9, study_group_name: "Г-9" },
        last_message: messages[3], last_message_at: date, unread_count: Math.min(unreadCount, 1) },
      { id: 12, user: { id: 2, full_name: "Иван Иванов", study_group_id: null, study_group_name: null },
        last_message: { text: "Привет" }, last_message_at: date, unread_count: Math.max(0, unreadCount - 1) },
    ];
    const state = { unread: unreadCount, conversations, messages, log: [],
      sendFailures: 1, accepted: new Map(), opened: [], read: [], newId: 60, delayed: "Анна" };
    window.__inboxFixture = state;
    configureInboxClient({
      async summary() { return { unread_count: state.unread }; },
      async conversations({ query, study_group_id, cursor, limit }) {
        state.log.push({ query, study_group_id, cursor, limit });
        if (query === state.delayed) await new Promise((done) => setTimeout(done, 850));
        if (query || study_group_id)
          return { items: state.conversations.filter((item) =>
            item.user.full_name.toLowerCase().includes(query.toLowerCase()) &&
            (!study_group_id || String(item.user.study_group_id) === String(study_group_id))),
          next_cursor: null };
        if (cursor === "page2") return { items: [state.conversations[1]], next_cursor: null };
        return { items: [state.conversations[0]], next_cursor: "page2" };
      },
      async open(userId) {
        state.opened.push(userId);
        let item = state.conversations.find((entry) => entry.user.id === userId);
        if (!item) {
          item = { id: 13, user: { id: userId, full_name: "Новый Пользователь" },
            last_message: null, unread_count: 0 };
          state.conversations.unshift(item);
        }
        return { conversation_id: item.id };
      },
      async messages(conversationId, { before }) {
        state.log.push({ conversationId, before });
        if (conversationId !== 11) return { items: [], next_cursor: null };
        if (before === "older") return {
          items: [{
            id: 2, conversation_id: 11, direction: "incoming", kind: "text",
            text: "Раннее сообщение", caption: null, created_at: date,
            author_admin_telegram_id: null, telegram_message_id: 100,
            reply_to_telegram_message_id: null, delivery_status: null,
            delivery_error_code: null, attachment: null,
          }], next_cursor: null,
        };
        return { items: state.messages.slice(), next_cursor: "older" };
      },
      async send(conversationId, payload) {
        state.log.push({ send: payload, conversationId });
        await new Promise((done) => setTimeout(done, 90));
        let accepted = state.accepted.get(payload.idempotency_key);
        if (accepted) {
          if (accepted.text !== payload.text ||
            accepted.reply_to_message_id !== payload.reply_to_message_id)
            throw new Error("409: Idempotency key reused with different payload");
        } else {
          const message = {
            id: state.newId++, conversation_id: conversationId, direction: "outgoing",
            kind: "text", text: payload.text, caption: null, created_at: date,
            author_admin_telegram_id: 123, telegram_message_id: null,
            reply_to_telegram_message_id: payload.reply_to_message_id || null,
            delivery_status: "sent", delivery_error_code: null, attachment: null,
          };
          accepted = { text: payload.text, reply_to_message_id: payload.reply_to_message_id, message };
          state.accepted.set(payload.idempotency_key, accepted);
          state.messages.push(message);
        }
        if (state.sendFailures-- > 0) throw new Error("Connection lost after HTTP 202");
        return { message: accepted.message };
      },
      async read(conversationId, lastReadMessageId) {
        state.read.push({ conversationId, lastReadMessageId });
        state.unread = 0;
        return { last_read_message_id: lastReadMessageId, unread_count: 0 };
      },
    });
  }, unread);
  await page.locator("#inbox-open").waitFor({ state: "visible" });
}

test("Inbox appears after admin authorization and preserves four payout tabs", async () => {
  await withPage(async ({ page, requests }) => {
    await page.locator("#inbox-open").waitFor({ state: "visible" });
    assert.equal(await page.locator("#inbox-dialog").isVisible(), false);
    assert.deepEqual((await page.locator("[data-mobile-view]").allTextContents()).map((name) => name.trim()),
      ["Создать", "Выбор", "Получатели", "История"]);
    for (const tab of ["composer", "users", "recipients", "payouts"]) {
      await page.locator('[data-mobile-view="' + tab + '"]').click();
      assert.equal(await page.locator('[data-screen="' + tab + '"]').isVisible(), true);
    }
    assert.equal(requests.some((req) => req.includes("/conversations/summary")), true);
    await page.waitForFunction(() => document.querySelectorAll("[data-inbox-write]").length === 3);
    assert.equal(await page.locator("[data-inbox-write]").count(), 3);
  });
});

test("Inbox: responsive layout, themes, focus, Escape and Telegram BackButton", async () => {
  await withPage(async ({ page }) => {
    await installFixture(page);
    await page.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      state.selectedPayoutId = 42;
      state.selectedUsers = new Set([1, 2]);
      state.selectedUsersByPayout.set(42, new Set([1, 2]));
    });
    await page.locator("#inbox-open").click();
    await page.locator(".inbox-conversation").first().waitFor();
    await mkdir(evidence, { recursive: true });
    for (const theme of ["light", "dark"]) {
      await page.evaluate((scheme) => {
        const tg = window.Telegram.WebApp;
        tg.colorScheme = scheme;
        for (const listener of tg.__callbacks.themeChanged || []) listener();
      }, theme);
      for (const width of [320, 390, 768, 1280]) {
        await page.setViewportSize({ width, height: 844 });
        await page.evaluate(() => {
          window.Telegram.WebApp.viewportHeight = 844;
          for (const listener of window.Telegram.WebApp.__callbacks.viewportChanged || []) listener();
        });
        const box = await page.locator("#inbox-dialog").boundingBox();
        assert.ok(box && box.x >= -1 && box.x + box.width <= width + 1,
          "Inbox overflows viewport at " + width + "px");
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await page.screenshot({ path: resolve(evidence, "inbox-" + theme + "-" + width + ".png") });
      }
    }
    await page.keyboard.press("Escape");
    await page.locator("#inbox-dialog").waitFor({ state: "hidden" });
    assert.equal(await page.locator("#inbox-open").evaluate((node) => document.activeElement === node), true);
    await page.locator("#inbox-open").click();
    await page.evaluate(() => window.Telegram.WebApp.BackButton.fire());
    await page.locator("#inbox-dialog").waitFor({ state: "hidden" });
    assert.equal(await page.evaluate(() => window.Telegram.WebApp.BackButton.count), 0);
    const selected = await page.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      return { selected: [...state.selectedUsers], byPayout: [...state.selectedUsersByPayout.get(42)] };
    });
    assert.deepEqual(selected, { selected: [1, 2], byPayout: [1, 2] });
  });
});

test("Inbox: empty list, per-admin unread, cursor, group filter and stale search", async () => {
  await withPage(async ({ page }) => {
    await installFixture(page, 7);
    assert.equal(await page.locator("#inbox-unread").innerText(), "7");
    await page.locator("#inbox-open").click();
    await page.locator("#inbox-conversations .inbox-conversation").first().waitFor();
    assert.equal(await page.locator("#inbox-conversations .inbox-conversation").count(), 1);
    await page.locator("#inbox-more").click();
    await page.waitForFunction(() => document.querySelectorAll("#inbox-conversations .inbox-conversation").length === 2);
    await page.locator("#inbox-search").fill("Анна");
    await page.waitForFunction(() => window.__inboxFixture.log.some((entry) => entry.query === "Анна"));
    await page.locator("#inbox-search").fill("Иван");
    await page.locator("#inbox-conversations").getByText("Иван Иванов").waitFor();
    await page.waitForTimeout(950);
    assert.equal(await page.locator("#inbox-conversations").getByText("Анна Петрова").count(), 0);
    await page.locator("#inbox-search").fill("");
    await page.locator("#inbox-group").selectOption("9");
    await page.locator("#inbox-conversations").getByText("Анна Петрова").waitFor();
    assert.equal(await page.locator("#inbox-conversations .inbox-conversation").count(), 1);
    await page.locator("#inbox-search").fill("Неизвестный");
    await page.locator("#inbox-list-status").getByText("Диалогов пока нет.").waitFor();

    await page.evaluate(async () => {
      const { configureInboxClient } = await import("/miniapp/js/inbox.js");
      configureInboxClient(null);
      configureInboxClient({
        summary: async () => ({ unread_count: 2 }),
        conversations: async () => ({ items: [], next_cursor: null }),
        open: async () => ({ conversation_id: 1 }),
        messages: async () => ({ items: [], next_cursor: null }),
        send: async () => ({}),
        read: async () => ({ last_read_message_id: 0, unread_count: 0 }),
      });
    });
    assert.equal(await page.locator("#inbox-unread").innerText(), "2");
  });
});

test("Inbox: history, read marker, older messages, incoming refresh, send/retry and mobile keyboard", async () => {
  await withPage(async ({ page }) => {
    await installFixture(page);
    await page.locator("#inbox-open").click();
    await page.locator("#inbox-conversations .inbox-conversation").first().click();
    await page.locator("#inbox-history [data-inbox-message='54']").waitFor();
    assert.match(await page.locator("#inbox-history").innerText(), /В очереди/);
    assert.match(await page.locator("#inbox-history").innerText(), /Отправляется/);
    assert.match(await page.locator("#inbox-history").innerText(), /Ошибка отправки/);
    assert.match(await page.locator("#inbox-history").innerText(), /Администратор · ID 456/);
    assert.match(await page.locator("#inbox-history").innerText(), /Ответ на Telegram #880/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.locator("#inbox-older").click();
    await page.locator("#inbox-history").getByText("Раннее сообщение").waitFor();
    assert.equal(await page.locator("#inbox-history [data-inbox-message]").count(), 5);

    await page.locator("#inbox-history [data-inbox-reply='51']").click();
    assert.match(await page.locator("#inbox-reply").innerText(), /Ответ на/);
    await page.locator("#inbox-text").fill("Первый ответ");
    await page.locator("#inbox-send").click();
    await page.waitForFunction(() =>
      [...document.querySelectorAll(".inbox-message")].some((node) =>
        node.textContent.includes("Первый ответ") && node.textContent.includes("Ошибка отправки")));
    await page.locator("#inbox-history .inbox-message").filter({ hasText: "Первый ответ" })
      .locator("[data-inbox-retry]").click();
    await page.locator("#inbox-history .inbox-message").filter({ hasText: "Первый ответ" })
      .filter({ hasText: "Отправлено" }).waitFor();
    const sent = await page.evaluate(() => {
      const fixture = window.__inboxFixture;
      return { requests: fixture.log.filter((entry) => entry.send),
        accepted: [...fixture.accepted.values()].map((entry) => entry.message.id) };
    });
    assert.equal(sent.requests.length, 2);
    assert.equal(sent.requests[0].send.reply_to_message_id, 51);
    assert.deepEqual(sent.requests[0].send, sent.requests[1].send,
      "An uncertain response must be retried with the original key and payload");
    assert.equal(sent.accepted.length, 1, "Backend accepted only one message");
    await page.waitForFunction(() => [...document.querySelectorAll("#inbox-history .inbox-message")]
      .filter((node) => node.textContent.includes("Первый ответ")).length === 1);
    assert.equal(await page.locator("#inbox-history .inbox-message").filter({ hasText: "Первый ответ" }).count(), 1);
    assert.equal(await page.locator("#inbox-history .inbox-message").filter({ hasText: "Первый ответ" }).locator("[data-inbox-retry]").count(), 0);
    assert.equal(await page.locator("#inbox-history [data-inbox-retry='54']").innerText(),
      "Отправить заново", "A server-side failure needs an explicit new send");

    await page.evaluate(() => {
      const fixture = window.__inboxFixture;
      fixture.messages.push({ ...fixture.messages[0], id: 55, text: "Новое входящее", telegram_message_id: 900 });
      const list = document.getElementById("inbox-history");
      list.scrollTop = 90;
    });
    await page.locator("#inbox-refresh").click();
    await page.locator("#inbox-history").getByText("Новое входящее").waitFor();
    assert.equal(await page.locator("#inbox-history [data-inbox-message='51']").count(), 1,
      "Refresh must de-duplicate existing messages");
    assert.ok(await page.evaluate(() => window.__inboxFixture.read.length > 0));

    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#inbox-text").focus();
    await page.evaluate(() => {
      window.Telegram.WebApp.viewportHeight = 530;
      for (const listener of window.Telegram.WebApp.__callbacks.viewportChanged || []) listener();
    });
    const box = await page.locator("#inbox-dialog").boundingBox();
    assert.ok(box && box.height <= 531, "Keyboard shrinks the dialog");
    const composer = await page.locator("#inbox-composer").boundingBox();
    assert.ok(composer && composer.y + composer.height <= box.y + box.height + 1,
      "Composer must remain inside the visible area");
    await mkdir(evidence, { recursive: true });
    await page.screenshot({ path: resolve(evidence, "inbox-keyboard.png") });
  });
});

test("Write action opens existing or empty conversation without changing payout selection", async () => {
  await withPage(async ({ page }) => {
    await installFixture(page);
    await page.locator('[data-mobile-view="users"]').click();
    await page.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      state.selectedPayoutId = 44;
      state.selectedUsers = new Set([1, 2]);
      state.selectedUsersByPayout.set(44, new Set([1, 2]));
      const { renderUsers } = await import("/miniapp/js/render-users.js");
      renderUsers();
    });
    await page.locator('[data-inbox-write="3"]').click();
    await page.locator("#inbox-title").getByText("Новый Пользователь").waitFor();
    assert.equal(await page.locator("#inbox-history-status").innerText(), "Сообщений пока нет.");
    assert.deepEqual(await page.evaluate(() => window.__inboxFixture.opened), [3]);
    assert.deepEqual(await page.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      return [ [...state.selectedUsers], [...state.selectedUsersByPayout.get(44)] ];
    }), [[1, 2], [1, 2]]);
  });
});


test("Unread stays unread when history is scrolled up during refresh", async () => {
  await withPage(async ({ page }) => {
    await installFixture(page);
    await page.locator("#inbox-open").click();
    await page.locator("#inbox-conversations .inbox-conversation").first().click();
    await page.locator("#inbox-history [data-inbox-message='54']").waitFor();
    await page.locator("#inbox-older").click();
    await page.locator("#inbox-history [data-inbox-message='2']").waitFor();
    await page.locator("#inbox-history [data-inbox-message='51']").scrollIntoViewIfNeeded();
    await page.evaluate(() => document.getElementById("inbox-history").dispatchEvent(new Event("scroll")));
    await page.waitForFunction(() => window.__inboxFixture.read.length > 0);
    const scrollHeight = await page.evaluate(() => {
      const list = document.getElementById("inbox-history");
      list.scrollTop = 0;
      return list.scrollHeight - list.clientHeight;
    });
    assert.ok(scrollHeight > 100, "Fixture history must actually be scrollable");
    const baselineReads = await page.evaluate(() => {
      const fixture = window.__inboxFixture;
      fixture.unread = 1;
      fixture.conversations[0].unread_count = 1;
      fixture.messages.push({
        ...fixture.messages[0], id: 55,
        text: "Новое входящее при чтении старой истории",
        telegram_message_id: 900,
      });
      return fixture.read.length;
    });
    await page.locator("#inbox-refresh").click();
    await page.locator("#inbox-history [data-inbox-message='55']").waitFor();
    await page.waitForTimeout(100);
    const beforeScroll = await page.evaluate(() => ({
      calls: window.__inboxFixture.read.length,
      unread: window.__inboxFixture.unread,
      scrollTop: document.getElementById("inbox-history").scrollTop,
    }));
    assert.equal(beforeScroll.calls, baselineReads,
      "A refresh while scrolled up must not advance the read marker");
    assert.equal(beforeScroll.unread, 1);
    assert.ok(beforeScroll.scrollTop < 50, "Refresh should preserve the top-of-history position");

    await page.evaluate(() => {
      const list = document.getElementById("inbox-history");
      list.scrollTop = list.scrollHeight;
    });
    await page.waitForFunction(() =>
      window.__inboxFixture.read.some((item) => item.lastReadMessageId === 55));
    assert.equal(await page.evaluate(() => window.__inboxFixture.unread), 0);
  });
});

test("Failed retry remains one bubble after a second timeout and keeps its original request", async () => {
  await withPage(async ({ page }) => {
    await installFixture(page);
    await page.evaluate(() => { window.__inboxFixture.sendFailures = 2; });
    await page.locator("#inbox-open").click();
    await page.locator("#inbox-conversations .inbox-conversation").first().click();
    await page.locator("#inbox-history [data-inbox-message='54']").waitFor();
    await page.locator("#inbox-text").fill("Три попытки — одно сообщение");
    await page.locator("#inbox-send").click();
    const bubble = page.locator(".inbox-message").filter({ hasText: "Три попытки — одно сообщение" });
    await bubble.locator("[data-inbox-retry]").waitFor();
    await bubble.locator("[data-inbox-retry]").click();
    await bubble.locator("[data-inbox-retry]").waitFor();
    await bubble.locator("[data-inbox-retry]").click();
    await bubble.filter({ hasText: "Отправлено" }).waitFor();
    const state = await page.evaluate(() => ({
      attempts: window.__inboxFixture.log.filter((entry) => entry.send).map((entry) => entry.send),
      records: window.__inboxFixture.accepted.size,
    }));
    assert.equal(state.attempts.length, 3);
    assert.deepEqual(state.attempts[0], state.attempts[1]);
    assert.deepEqual(state.attempts[1], state.attempts[2]);
    assert.equal(state.records, 1);
    await page.waitForFunction(() => [...document.querySelectorAll("#inbox-history .inbox-message")]
      .filter((node) => node.textContent.includes("Три попытки — одно сообщение")).length === 1);
    assert.equal(await bubble.count(), 1);
    assert.equal(await bubble.locator("[data-inbox-retry]").count(), 0);
  });
});
