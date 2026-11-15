import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { extname, resolve, sep } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = resolve(fileURLToPath(new URL("../..", import.meta.url)), "frontend");
const evidence = process.env.BROWSER_EVIDENCE_DIR || resolve(tmpdir(), "topsbottg-browser-evidence");
const mime = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript" };
const tabs = ["composer", "users", "recipients", "payouts"];

async function withApp(width, theme, run) {
  const server = createServer(async (req, res) => {
    const pathname = new URL(req.url, "http://localhost").pathname;
    const file = resolve(root, pathname.replace(/^\/miniapp\/?/, "") || "index.html");
    if (file !== resolve(root, "index.html") && !file.startsWith(root + sep)) {
      res.writeHead(403).end();
      return;
    }
    try {
      res.writeHead(200, { "Content-Type": mime[extname(file)] || "application/octet-stream" });
      res.end(await readFile(file));
    } catch {
      res.writeHead(404).end();
    }
  });
  const browser = await chromium.launch({ headless: true });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  const page = await browser.newPage({ viewport: { width, height: 860 } });
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const fixture = {
    members: [
      { telegram_id: 123, full_name: "Владелец", role: "owner" },
      { telegram_id: 555, full_name: "Администратор Один", role: "admin" },
      { telegram_id: 777, full_name: "Администратор Два", role: "admin" },
    ],
    directoryMode: "ok",
    releaseDirectory: null,
    directoryCalls: 0,
    revoked: false,
  };
  try {
    await page.addInitScript((colorScheme) => {
      const callbacks = {};
      window.Telegram = {
        WebApp: {
          initData: "test-admin-init-data",
          colorScheme,
          themeParams: {},
          viewportHeight: innerHeight,
          safeAreaInset: { bottom: 0 },
          contentSafeAreaInset: { bottom: 0 },
          ready() {}, expand() {},
          onEvent(event, cb) { (callbacks[event] ??= []).push(cb); },
          BackButton: { onClick() {}, offClick() {}, show() {}, hide() {} },
        },
      };
    }, theme);
    await page.route("https://telegram.org/**", (route) => route.abort());
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      const method = route.request().method();
      if (path === "/api/admin/events" && method === "GET")
        return route.fulfill({ status: 200, contentType: "text/event-stream", body: ": connected\n\n" });
      if (path === "/api/admin/admin-directory") {
        fixture.directoryCalls += 1;
        if (fixture.directoryMode === "hold") {
          await new Promise((done) => { fixture.releaseDirectory = done; });
        }
        if (fixture.revoked) return route.fulfill({
          status: 403, contentType: "application/json", body: '{"detail":"Forbidden"}',
        });
        if (fixture.directoryMode === "error") return route.fulfill({
          status: 503, contentType: "application/json", body: '{"detail":"Service unavailable"}',
        });
        return route.fulfill({
          status: 200, contentType: "application/json", body: JSON.stringify(fixture.members),
        });
      }
      let result;
      if (path === "/api/admin/events-token" && method === "POST")
        result = { token: "test-token", expires_in: 300 };
      else if (path === "/api/admin/me") result = { telegram_user_id: 123, is_admin: true, role: "owner" };
      else if (path === "/api/admin/conversations/summary") result = { unread_count: 0 };
      else if (path === "/api/admin/admin-invitations") result = [];
      else if (path === "/api/admin/study-groups") result = [];
      else if (path === "/api/admin/users") result = { items: [], offset: 0, has_more: false };
      else if (path === "/api/admin/payouts") result = [];
      else return route.fulfill({ status: 501, contentType: "application/json", body: "{}" });
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(result) });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/miniapp/`);
    await page.locator("#admins-open").waitFor({ state: "visible" });
    await page.locator("#inbox-open").waitFor({ state: "visible" });
    await page.evaluate((selectedTheme) => document.documentElement.dataset.theme = selectedTheme, theme);
    await mkdir(evidence, { recursive: true });
    await run({ page, fixture });
    assert.deepEqual(errors, [], "Uncaught browser errors");
  } finally {
    fixture.releaseDirectory?.();
    await page.close();
    await browser.close();
    if (server.listening) await new Promise((done) => server.close(done));
  }
}

async function headerCoordinates(page) {
  return page.evaluate(() => {
    const bounding = (selector) => {
      const { x, y, width, height } = document.querySelector(selector).getBoundingClientRect();
      return { x, y, width, height };
    };
    return {
      inbox: bounding("#inbox-open"),
      admins: bounding("#admins-open"),
      docWidth: document.documentElement.scrollWidth,
      viewport: innerWidth,
      pageHeight: document.documentElement.scrollHeight,
    };
  });
}

function assertStableHeader(reference, current, label) {
  for (const key of ["inbox", "admins"]) {
    assert.ok(Math.abs(reference[key].x - current[key].x) <= 1,
      `Header ${key} moved on ${label}: ${JSON.stringify({ reference, current })}`);
    assert.ok(Math.abs(reference[key].width - current[key].width) <= 1,
      `Header ${key} resized on ${label}`);
  }
  assert.ok(current.docWidth <= current.viewport,
    `Horizontal overflow on ${label}: ${JSON.stringify(current)}`);
}

async function selectedPayout(page, selected) {
  await page.evaluate(async (value) => {
    const { state } = await import("/miniapp/js/store.js");
    const { renderApp } = await import("/miniapp/js/render-app.js");
    state.selectedPayoutId = value ? 917 : null;
    state.selectedPayoutDetail = value ? {
      payout: {
        id: 917, period_start_day: 1, period_start_month: 11,
        period_end_day: 15, period_end_month: 11,
        status: "sent", workflow_state: "sent",
        paid_recipient_count: 1, active_recipient_count: 3,
      },
    } : null;
    state.recipients = value ? [
      { id: 81, full_name: "Получатель", status: "payment_received" },
      { id: 82, full_name: "Другой получатель", status: "paid" },
    ] : [];
    renderApp();
  }, selected);
}

for (const width of [320, 390, 768, 1280]) {
  for (const theme of ["light", "dark"]) {
    test(`four stable tabs and payout empty states ${width}px ${theme}`, async () => {
      await withApp(width, theme, async ({ page }) => {
        const base = await headerCoordinates(page);
        await page.evaluate(async () => {
          const { state } = await import("/miniapp/js/store.js");
          const { renderApp } = await import("/miniapp/js/render-app.js");
          state.payouts = Array.from({ length: 64 }, (_, i) => ({
            id: i + 1, status: "sent", workflow_state: "sent",
            period_start_day: 1, period_start_month: 11,
            period_end_day: 15, period_end_month: 11,
            recipient_count: 4,
          }));
          state.users = Array.from({ length: 55 }, (_, i) => ({
            id: i + 1, full_name: `Получатель ${i + 1}`,
            telegram_user_id: i + 500,
          }));
          renderApp();
        });
        for (const tab of tabs) {
          await page.evaluate(() => window.scrollTo(0, 0));
          await page.locator(`[data-mobile-view="${tab}"]`).click();
          await page.locator(`[data-screen="${tab}"]`).waitFor({ state: "visible" });
          assertStableHeader(base, await headerCoordinates(page), `${width} ${theme} ${tab}`);
          await page.screenshot({ path: resolve(evidence, `tabs-${width}-${theme}-${tab}.png`) });
        }
        // The unread badge and a long title may not displace either header action.
        await page.evaluate(() => {
          const badge = document.querySelector("#inbox-unread");
          badge.textContent = "999+";
          badge.classList.remove("hidden");
          document.querySelector("#screen-title").textContent =
            "Очень длинное название раздела, которое переносится на несколько строк";
        });
        assertStableHeader(base, await headerCoordinates(page), `badge/title ${width} ${theme}`);
        await page.evaluate(() => {
          document.querySelector("#inbox-unread").classList.add("hidden");
        });

        await page.locator('[data-mobile-view="payouts"]').click();
        const history = page.locator("#history-current-context");
        assert.equal(await history.isVisible(), false);
        assert.ok((await page.locator("#payouts-list .payout-row").count()) > 0,
          "History list must remain visible without selected payout");
        await page.screenshot({ path: resolve(evidence, `history-empty-${width}-${theme}.png`) });
        await page.locator('[data-mobile-view="recipients"]').click();
        assert.equal(await page.locator("#recipient-summary").isVisible(), false);
        assert.equal(await page.locator("#recipient-filters").isVisible(), false);
        assert.equal(await page.locator("#recipients-list").isVisible(), false);
        assert.equal(await page.locator("#recipients-context").isVisible(), false);
        assert.equal(await page.locator("#recipients-state button").count(), 1);
        assert.equal((await page.locator("#recipients-state").innerText()).trim(), "Открыть историю");
        assert.equal(await page.locator("#recipients-state").evaluate((node) =>
          getComputedStyle(node).backgroundColor === "rgba(0, 0, 0, 0)"), true);
        await page.screenshot({ path: resolve(evidence, `recipients-empty-${width}-${theme}.png`) });
        await page.locator('#recipients-state [data-action="open-history"]').click();
        assert.equal(await page.locator('[data-screen="payouts"]').isVisible(), true);

        await selectedPayout(page, true);
        assert.equal(await history.isVisible(), true);
        assert.equal(await history.locator('[data-action="clear-payout"]').count(), 1);
        assert.equal(await history.locator('[data-action="delete-payout"]').count(), 1);
        await page.screenshot({ path: resolve(evidence, `history-selected-${width}-${theme}.png`) });
        await page.locator('[data-mobile-view="recipients"]').click();
        assert.equal(await page.locator("#recipient-summary").isVisible(), true);
        assert.equal(await page.locator("#recipient-filters").isVisible(), true);
        assert.equal(await page.locator("#recipients-list").isVisible(), true);
        assert.equal(await page.locator("#recipients-list [data-recipient-row]").count(), 2);
        await page.screenshot({ path: resolve(evidence, `recipients-selected-${width}-${theme}.png`) });

        await page.locator('[data-mobile-view="payouts"]').click();
        await page.locator('[data-action="clear-payout"]').click();
        assert.equal(await history.isVisible(), false);
        assert.ok((await page.locator("#payouts-list .payout-row").count()) > 0);
        await page.locator('[data-mobile-view="recipients"]').click();
        assert.equal((await page.locator("#recipients-state").innerText()).trim(), "Открыть историю");
        assertStableHeader(base, await headerCoordinates(page), `clear selection ${width} ${theme}`);
      });
    });
  }
}

function modalBoxEqual(a, b, label) {
  assert.ok(a && b, `Missing modal box on ${label}`);
  for (const part of ["x", "y", "width", "height"])
    assert.ok(Math.abs(a[part] - b[part]) <= 1,
      `Admin dialog ${part} changed on ${label}: ${JSON.stringify({ a, b })}`);
}

for (const width of [390, 1280]) {
  for (const theme of ["light", "dark"]) {
    test(`admin dialog stable during load/reopen/error/refresh ${width}px ${theme}`, async () => {
      await withApp(width, theme, async ({ page, fixture }) => {
        fixture.directoryMode = "hold";
        await page.locator("#admins-open").click();
        const modal = page.locator("#admins-dialog");
        await modal.waitFor({ state: "visible" });
        await page.locator(".admins-member-placeholder").first().waitFor();
        assert.equal(await page.locator(".admins-member-placeholder").count(), 3);
        const initial = await modal.boundingBox();
        if (width < 760) {
          assert.ok(initial && Math.abs(initial.x) <= 1 && Math.abs(initial.width - width) <= 1,
            `Mobile modal must fill viewport: ${JSON.stringify(initial)}`);
        }
        await page.screenshot({ path: resolve(evidence, `admins-loading-${width}-${theme}.png`) });
        fixture.directoryMode = "ok";
        fixture.releaseDirectory?.();
        fixture.releaseDirectory = null;
        await page.waitForFunction(() => document.querySelectorAll("#admins-members .admins-member").length === 3);
        modalBoxEqual(initial, await modal.boundingBox(), "first load");
        await page.screenshot({ path: resolve(evidence, `admins-loaded-${width}-${theme}.png`) });

        await page.locator("#admins-close").click();
        await modal.waitFor({ state: "hidden" });
        fixture.directoryMode = "hold";
        await page.locator("#admins-open").click();
        await modal.waitFor({ state: "visible" });
        assert.equal(await page.locator("#admins-members .admins-member").count(), 3,
          "Prior member list must remain during a refresh");
        assert.equal(await page.locator(".admins-member-placeholder").count(), 0,
          "Skeleton must not replace an already loaded list");
        modalBoxEqual(initial, await modal.boundingBox(), "reopen");
        fixture.directoryMode = "error";
        fixture.releaseDirectory?.();
        fixture.releaseDirectory = null;
        await page.waitForFunction(() =>
          document.querySelector("#admins-status")?.textContent.includes("Backend"));
        assert.equal(await page.locator("#admins-members .admins-member").count(), 3);
        modalBoxEqual(initial, await modal.boundingBox(), "failed refresh");
        await page.screenshot({ path: resolve(evidence, `admins-error-${width}-${theme}.png`) });

        fixture.directoryMode = "ok";
        fixture.members = fixture.members.slice(0, 2);
        await page.locator("#admins-refresh").click();
        await page.waitForFunction(() => document.querySelectorAll("#admins-members .admins-member").length === 2);
        assert.equal((await page.locator("#admins-status").innerText()).trim(), "");
        modalBoxEqual(initial, await modal.boundingBox(), "membership change");
        await page.screenshot({ path: resolve(evidence, `admins-refreshed-${width}-${theme}.png`) });

        fixture.revoked = true;
        await page.locator("#admins-refresh").click();
        await modal.waitFor({ state: "hidden" });
        assert.equal(await page.locator("#admins-open").isVisible(), false,
          "Revoked owner role must hide owner-only controls");
      });
    });
  }
}
