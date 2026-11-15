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

async function browserFixture(role, run) {
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, "http://localhost").pathname;
    const file = resolve(root, path.replace(/^\/miniapp\/?/, "") || "index.html");
    if (file !== resolve(root, "index.html") && !file.startsWith(root + sep)) {
      res.writeHead(403).end();
      return;
    }
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
    page.setDefaultTimeout(9000);
    const state = {
      members: [
        { telegram_id: 123, role: "owner", full_name: "Евгений Дмитриевич" },
        { telegram_id: 456, role: "owner", full_name: "Второй владелец" },
        { telegram_id: 555, role: "admin", full_name: "Администратор" },
      ],
      invitations: [],
      requests: [],
      directoryFailures: 0,
      searchFailures: 0,
      candidateRequests: [],
      delayedCandidateName: null,
    };
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.addInitScript((role) => {
      const callbacks = {};
      const backHandlers = new Set();
      window.Telegram = {
        WebApp: {
          initData: "test-admin-init-data",
          colorScheme: "light",
          themeParams: {},
          viewportHeight: innerHeight,
          safeAreaInset: { bottom: 0 },
          contentSafeAreaInset: { bottom: 0 },
          ready() {}, expand() {},
          onEvent(name, callback) { (callbacks[name] ??= []).push(callback); },
          __callbacks: callbacks,
          BackButton: {
            onClick(handler) { backHandlers.add(handler); },
            offClick(handler) { backHandlers.delete(handler); },
            show() {}, hide() {},
            fire() { for (const handler of backHandlers) handler(); },
            get listeners() { return backHandlers.size; },
          },
        },
      };
    }, role);
    await page.route("https://telegram.org/**", (route) => route.abort());
    await page.route("**/api/**", async (route) => {
      const req = route.request();
      const path = new URL(req.url()).pathname;
      const search = new URL(req.url()).searchParams;
      const method = req.method();
      state.requests.push(`${method} ${path}`);
      let data = null, code = 200;
      if (path === "/api/admin/events" && method === "GET") {
        return route.fulfill({
          status: 200, contentType: "text/event-stream", body: ": connected\\n\\n",
        });
      }
      if (path === "/api/admin/events-token" && method === "POST")
        data = { token: "fixture-token", expires_in: 300 };
      else if (path === "/api/admin/me" && method === "GET")
        data = { telegram_user_id: role === "owner" ? 123 : 555, is_admin: true, role };
      else if (path === "/api/admin/study-groups" && method === "GET")
        data = [{ id: 9, code: "М3О-207СВ-25" }];
      else if (path === "/api/admin/payouts" && method === "GET")
        data = [];
      else if (path === "/api/admin/users" && method === "GET")
        data = { items: [{ id: 1, full_name: "Тестовый получатель", study_group: null }],
          offset: 0, has_more: false };
      else if (path === "/api/admin/admin-directory" && method === "GET") {
        if (role !== "owner") code = 403;
        else if (state.directoryFailures > 0) {
          state.directoryFailures--;
          code = 503;
        } else data = state.members;
      } else if (path === "/api/admin/admin-invitations" && method === "GET")
        role === "owner" ? data = state.invitations : code = 403;
      else if (path === "/api/admin/admin-candidates" && method === "GET") {
        state.candidateRequests.push(req.url());
        if (state.delayedCandidateName === search.get("search"))
          await new Promise((done) => setTimeout(done, 750));
        if (role !== "owner") code = 403;
        else if (state.searchFailures > 0) {
          state.searchFailures--;
          code = 503;
        } else {
          const name = search.get("search");
          if (name === "Анна" || (name === null && search.get("study_group_id") === "9")) {
            const entries = [
              { telegram_id: 777, full_name: "Анна Петрова", study_group: null, role: null },
              { telegram_id: 778, full_name: "Анна Смирнова", study_group: "М3О-207СВ-25", role: null },
            ];
            const filtered = search.get("study_group_id") === "9" ? [entries[1]] : entries;
            const offset = Number(search.get("offset") || "0");
            const page = filtered.slice(offset, offset + 1);
            data = { items: page, has_more: offset + 1 < filtered.length };
          } else if (name === "Евгений") {
            data = { items: [{ telegram_id: 123, full_name: "Евгений",
              study_group: null, role: "owner" }], has_more: false };
          } else {
            data = { items: [], has_more: false };
          }
        }
      } else if (path === "/api/admin/admin-invitations" && method === "POST") {
        const payload = req.postDataJSON();
        if (role !== "owner") code = 403;
        else if (payload.target_telegram_id !== 777 ||
          Object.hasOwn(payload, "expected_username")) code = 409;
        else {
          state.invitations = [{
            id: 42, target_telegram_id: 777, status: "pending",
            delivered_at: null, delivery_state: "queued", full_name: "Анна Петрова",
          }];
          data = { id: 42, target_telegram_id: 777, status: "pending" };
          code = 201;
        }
      } else if (path === "/api/admin/admin-invitations/42/cancel" && method === "POST") {
        if (role !== "owner") code = 403;
        else {
          state.invitations = [];
          data = { status: "cancelled" };
        }
      } else if (path === "/api/admin/admin-grants/555" && method === "DELETE") {
        if (role !== "owner") code = 403;
        else {
          state.members = state.members.filter((member) => member.telegram_id !== 555);
          code = 204;
        }
      } else code = 501;

      if (code === 204) return route.fulfill({ status: code, body: "" });
      return route.fulfill({
        status: code,
        contentType: "application/json",
        body: JSON.stringify(data ?? { detail: code === 503 ? "Service unavailable" : "Forbidden" }),
      });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/miniapp/`);
    await page.locator('[data-screen="composer"]').waitFor({ state: "visible" });
    await page.waitForFunction(async () => {
      const { state } = await import("/miniapp/js/store.js");
      return state.adminRole !== null;
    });
    await run({ page, state, role });
    assert.deepEqual(pageErrors, [], "Unexpected browser errors");
    assert.equal(state.requests.filter((request) => request.endsWith("/api/admin/me")).length, 1);
    assert.equal(state.requests.some((request) => request.includes("undefined")), false);
  } finally {
    await browser.close();
    if (server.listening) await new Promise((done) => server.close(done));
  }
}

test("owner can invite, cancel, revoke; modal retains payout state and supports BackButton", async () => {
  await browserFixture("owner", async ({ page, state }) => {
    const snapshot = await page.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      const button = document.querySelector("#admins-open");
      return {
        role: state.adminRole,
        buttonClass: button?.className,
        buttonDisplay: button ? getComputedStyle(button).display : null,
        ancestorHidden: button?.closest("[hidden], .hidden")?.outerHTML.slice(0, 180),
      };
    });
    assert.equal(snapshot.role, "owner",
      JSON.stringify({ snapshot, requests: state.requests }));
    assert.notEqual(snapshot.buttonDisplay, "none",
      JSON.stringify({ snapshot, requests: state.requests }));
    await page.locator("#admins-open").waitFor({ state: "visible" });
    await page.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      state.selectedPayoutId = 88;
      state.selectedUsers = new Set([1, 2]);
      state.selectedUsersByPayout.set(88, new Set([1, 2]));
      state.composer.messageBefore = "Сохранённый текст";
    });
    await page.locator("#admins-open").focus();
    await page.locator("#admins-open").click();
    const modal = page.locator("#admins-dialog");
    assert.equal(await modal.evaluate((node) => node.open), true);
    await page.locator(".admins-member").first().waitFor();
    assert.equal(await page.locator("#admins-members .admins-member").count(), 3);
    assert.equal(await page.locator("#admins-members [data-admin-action='revoke']").count(), 1);
    assert.doesNotMatch(await page.locator("#admins-members").innerText(), /ADMIN_TELEGRAM_IDS|\.env|555/);

    assert.equal(await page.locator("#admins-search-form").count(), 0);
    assert.equal(await page.getByRole("button", { name: "Найти", exact: true }).count(), 0);
    state.searchFailures = 1;
    await page.locator("#admins-search-input").fill("Анна");
    await page.waitForFunction(() => document.querySelector("#admins-search-error")?.textContent.includes("Backend"));
    await page.locator("#admins-search-input").fill("Анн");
    await page.locator("#admins-search-input").fill("Анна");
    await page.locator("#admins-candidate").getByText("Анна Петрова").waitFor();
    assert.match(await page.locator("#admins-candidate").innerText(), /Группа не указана/);
    await page.locator("#admins-candidate [data-admin-action='more-candidates']").click();
    await page.locator("#admins-candidate").getByText("Анна Смирнова").waitFor();
    assert.equal(await page.locator("#admins-candidate .admins-candidate-card").count(), 2);

    const candidateBox = await page.locator(".admins-candidate-card").first().boundingBox();
    assert.ok(candidateBox && candidateBox.height < 160,
      `Candidate card is unexpectedly tall: ${candidateBox?.height}`);
    await page.locator("#admins-candidate .admins-candidate-card").first().locator("[data-admin-action='invite']").click();
    assert.equal(state.invitations.length, 0);
    await page.locator("#admins-candidate [data-admin-action='confirm-invite']").click();
    await page.locator("#admins-pending").getByText("В очереди на отправку").waitFor();
    assert.equal(state.invitations.length, 1);
    state.invitations[0].delivery_state = "unknown";
    await page.locator("#admins-refresh").click();
    await page.locator("#admins-pending").getByText("Результат отправки неизвестен").waitFor();
    state.invitations[0].delivered_at = new Date().toISOString();
    state.invitations[0].delivery_state = "delivered";
    await page.locator("#admins-refresh").click();
    await page.locator("#admins-pending").getByText("Ожидает ответа").waitFor();
    await page.locator("#admins-pending [data-admin-action='cancel-invite']").click();
    await page.locator("#admins-pending").getByText("Нет ожидающих приглашений.").waitFor();
    assert.equal(state.invitations.length, 0);
    await page.locator("#admins-search-group").selectOption("9");
    await page.locator("#admins-candidate").getByText("Анна Смирнова").waitFor();
    assert.equal(await page.locator("#admins-candidate .admins-candidate-card").count(), 1);
    const beforeGroupOnly = state.candidateRequests.length;
    await page.locator("#admins-search-input").fill("");
    await page.waitForFunction(() =>
      document.querySelector("#admins-candidate")?.textContent.includes("Анна Смирнова"));
    assert.equal(state.candidateRequests.length > beforeGroupOnly, true);
    await page.locator("#admins-search-group").selectOption("");
    await page.locator("#admins-search-input").fill("Евгений");
    await page.locator("#admins-candidate").getByText("Уже владелец").waitFor();
    assert.equal(await page.locator("#admins-candidate [data-admin-action='invite']").count(), 0);

    state.directoryFailures = 1;
    await page.locator("#admins-refresh").click();
    await page.waitForFunction(() => document.querySelector("#admins-status")?.textContent.includes("Backend"));
    await page.locator("#admins-refresh").click();
    await page.waitForFunction(() => !document.querySelector("#admins-status")?.textContent.includes("Backend"));

    await page.locator("#admins-members [data-admin-action='revoke']").click();
    assert.equal(state.members.length, 3, "A single click must not revoke");
    await page.locator("#admins-members [data-admin-action='confirm-revoke']").click();
    await page.waitForFunction(() => document.querySelectorAll("#admins-members .admins-member").length === 2);
    assert.equal(state.members.length, 2);
    assert.equal(await page.locator("#admins-members [data-admin-action='revoke']").count(), 0);

    state.members.push({
      telegram_id: 777, role: "admin", full_name: "Анна Петрова",
    });
    await page.locator("#admins-refresh").click();
    await page.locator("#admins-members").getByText("Анна Петрова").waitFor();

    await mkdir(evidence, { recursive: true });
    for (const theme of ["light", "dark"]) {
      await page.evaluate((scheme) => {
        const webApp = window.Telegram.WebApp;
        webApp.colorScheme = scheme;
        for (const cb of webApp.__callbacks.themeChanged || []) cb();
      }, theme);
      for (const width of [320, 390, 768]) {
        await page.setViewportSize({ width, height: 844 });
        const box = await modal.boundingBox();
        assert.ok(box && box.x >= -1 && box.x + box.width <= width + 1,
          `Admin dialog outside viewport at ${width}px: ${JSON.stringify(box)}`);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
        assert.equal(overflow, false, `Horizontal overflow at ${width}px`);
        await page.screenshot({ path: resolve(evidence, `admin-dialog-${theme}-${width}.png`) });
      }
    }
    await page.keyboard.press("Escape");
    await modal.waitFor({ state: "hidden" });
    assert.equal(await page.locator("#admins-open").evaluate((el) => el === document.activeElement), true);
    const payout = await page.evaluate(async () => {
      const { state } = await import("/miniapp/js/store.js");
      return {
        id: state.selectedPayoutId,
        users: [...state.selectedUsers],
        stored: [...state.selectedUsersByPayout.get(88)],
        before: state.composer.messageBefore,
        view: state.activeMobileView,
      };
    });
    assert.deepEqual(payout, {
      id: 88, users: [1, 2], stored: [1, 2],
      before: "Сохранённый текст", view: "composer",
    });

    await page.locator("#admins-open").click();
    await page.locator("#admins-dialog").waitFor({ state: "visible" });
    await page.evaluate(() => window.Telegram.WebApp.BackButton.fire());
    await modal.waitFor({ state: "hidden" });
    assert.equal(await page.evaluate(() => window.Telegram.WebApp.BackButton.listeners), 0);
    assert.equal(await page.locator('[data-mobile-view]').count(), 4);
  });
});

test("live search discards stale responses and clears results", async () => {
  await browserFixture("owner", async ({ page, state }) => {
    await page.locator("#admins-open").click();
    state.delayedCandidateName = "Анна";
    const input = page.locator("#admins-search-input");
    await input.fill("Анна");
    await page.waitForFunction(() =>
      document.querySelector("#admins-search-error")?.textContent.includes("Ищем"));
    await input.fill("Евгений");
    await page.locator("#admins-candidate").getByText("Уже владелец").waitFor();
    await page.waitForTimeout(850);
    assert.equal(await page.locator("#admins-candidate").getByText("Анна Петрова").count(), 0);
    assert.equal(await page.locator("#admins-candidate .admins-candidate-card").count(), 1);

    await input.fill("");
    assert.equal(await page.locator("#admins-candidate .admins-candidate-card").count(), 0);
    const count = state.candidateRequests.length;
    await input.fill("А");
    await page.waitForTimeout(400);
    assert.equal(state.candidateRequests.length, count, "One-letter names must not start requests");
  });
});

test("regular admin cannot access owner controls or owner-only endpoints", async () => {
  await browserFixture("admin", async ({ page, state }) => {
    await page.waitForFunction(async () => {
      const { state } = await import("/miniapp/js/store.js");
      return state.adminRole === "admin";
    });
    assert.equal(await page.locator("#admins-open").isVisible(), false);
    assert.equal(await page.locator("#admins-dialog").isVisible(), false);
    assert.equal(state.requests.some((s) => s.includes("/api/admin/admin-directory")), false);
    const status = await page.evaluate(async () => {
      const response = await fetch("/api/admin/admin-directory", {
        headers: { "X-Telegram-Init-Data": "test-admin-init-data" },
      });
      return response.status;
    });
    assert.equal(status, 403);
    assert.equal(await page.locator('[data-mobile-view]').count(), 4);
  });
});
