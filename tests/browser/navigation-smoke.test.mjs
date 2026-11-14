import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { chromium } from "playwright";

const root = resolve(fileURLToPath(new URL("../..", import.meta.url)), "frontend");
const mime = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript" };
const tabIds = ["composer", "users", "recipients", "payouts"];

// Independent browser cases, unlike the full stateful payout workflow in frontend.test.mjs.
for (const [width, height] of [[320, 740], [390, 844], [768, 900]]) {
  test(`Mini App navigation remains usable at ${width}px`, async () => {
    const server = createServer(async (req, res) => {
      const url = new URL(req.url, "http://localhost");
      const file = resolve(root, url.pathname.replace(/^\/miniapp\/?/, "") || "index.html");
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
    const browser = await chromium.launch({ headless: true });
    try {
      await new Promise((done) => server.listen(0, "127.0.0.1", done));
      const page = await browser.newPage({ viewport: { width, height } });
      page.setDefaultTimeout(7000);
      const unexpected = [];
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.addInitScript(() => {
        window.Telegram = {
          WebApp: {
            initData: "test-admin-init-data",
            colorScheme: "light",
            themeParams: {},
            viewportHeight: innerHeight,
            safeAreaInset: { bottom: 0 },
            contentSafeAreaInset: { bottom: 0 },
            ready() {},
            expand() {},
            onEvent() {},
          },
        };
      });
      await page.route("https://telegram.org/**", (route) => route.abort());
      await page.route("**/api/**", (route) => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        let result = null;
        if (request.method() === "POST" && path === "/api/admin/events-token") {
          result = { token: "test-admin", expires_in: 300 };
        } else if (request.method() === "GET" && path === "/api/admin/payouts") {
          result = [];
        } else if (request.method() === "GET" && path === "/api/admin/study-groups") {
          result = [];
        } else if (request.method() === "GET" && path === "/api/admin/users") {
          result = { items: [], limit: 50, offset: 0, has_more: false };
        } else if (request.method() === "GET" && path === "/api/admin/me") {
          result = { telegram_user_id: 123, is_admin: true };
        } else if (request.method() === "GET" && path === "/api/admin/events") {
          return route.fulfill({ status: 200, contentType: "text/event-stream", body: ":connected\n\n" });
        }
        if (result === null) {
          unexpected.push(`${request.method()} ${path}`);
          return route.fulfill({ status: 501, contentType: "application/json", body: "{}" });
        }
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(result) });
      });

      await page.goto(`http://127.0.0.1:${server.address().port}/miniapp/`);
      await page.locator('[data-screen="composer"]').waitFor({ state: "visible" });
      assert.deepEqual(
        (await page.locator("[data-mobile-view]").allTextContents()).map((label) => label.trim()),
        ["Создать", "Выбор", "Получатели", "История"],
      );
      for (const tab of tabIds) {
        await page.locator(`[data-mobile-view="${tab}"]`).click();
        assert.equal(await page.locator(`[data-screen="${tab}"]`).isVisible(), true);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        const nav = await page.locator(".bottom-nav").boundingBox();
        assert.ok(nav && nav.x >= 0 && nav.y >= 0 &&
          nav.x + nav.width <= width + 1 && nav.y + nav.height <= height + 1,
          `Navigation outside viewport ${width}px: ${JSON.stringify(nav)}`);
      }
      assert.deepEqual(unexpected, [], "Unexpected API requests must not receive a fake success");
      assert.deepEqual(errors, [], "Mini App must not throw uncaught browser errors");
      await page.close();
    } finally {
      await browser.close();
      if (server.listening) await new Promise((done) => server.close(done));
    }
  });
}
