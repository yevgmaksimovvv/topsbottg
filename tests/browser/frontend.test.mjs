import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";
import { test } from "node:test";
import { chromium } from "playwright";

const repository = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const frontend = resolve(repository, "frontend");
const evidence = resolve(tmpdir(), "topsbottg-browser-evidence");
const mime = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
};

test("responsive Mini App and payout workflows", async (t) => {
  let users = [];
  let recipients = [];
  let sentRecipients = [];
  let renamedUserId = null;
  let historicalRecipients = [
    {
      id: 501,
      user_id: 1,
      full_name:
        "Александр Фамилия с очень длинным именем для проверки переноса строк",
      status: "payment_received",
      payment_profile_snapshot: {
        raw_payment_details: `Сбербанк · +7 (999) 123-45-67\nПолучатель: ${"Очень длинные данные ".repeat(5)}`,
      },
    },
    {
      id: 502,
      user_id: 2,
      full_name: "Мария Петрова",
      status: "failed",
      failure_reason: "Ошибка отправки",
    },
    {
      id: 503,
      user_id: 3,
      full_name: "Иван Иванов",
      status: "paid",
      payment_profile_snapshot: {
        raw_payment_details: "Номер карты 0000 0000 0000 0000",
      },
      paid_note: "выплачено",
    },
    ...Array.from({ length: 30 }, (_, index) => ({
      id: 700 + index,
      user_id: 20 + index,
      full_name: `Ожидающий получатель ${index + 1}`,
      status: "pending",
    })),
  ];
  const payouts = [
    {
      id: 38,
      status: "sent",
      period_start_day: 1,
      period_start_month: 11,
      period_end_day: 7,
      period_end_month: 11,
      period_label: "01.11 – 07.11",
    },
    {
      id: 40,
      status: "draft",
      period_start_day: 8,
      period_start_month: 11,
      period_end_day: 14,
      period_end_month: 11,
      period_label: "08.11 – 14.11",
    },
    {
      id: 37,
      status: "sent",
      period_start_day: 25,
      period_start_month: 10,
      period_end_day: 31,
      period_end_month: 10,
      period_label: "25.10 – 31.10",
    },
  ];
  const requestLog = [];
  const delayedRequests = new Map();
  const delayNextRequest = (path) => {
    let release;
    const promise = new Promise((resolve) => {
      release = resolve;
    });
    const queue = delayedRequests.get(path) || [];
    queue.push(promise);
    delayedRequests.set(path, queue);
    return { release: (result = null) => release(result) };
  };
  let sendCalls = 0;
  const sentUserIds = [];
  let markPaidCalls = 0;
  let createPayoutCalls = 0;
  const failedPayoutDetails = new Set();
  let server;
  let browser;

  server = createServer(async (request, response) => {
    const pathname = decodeURIComponent(
      new URL(request.url, "http://localhost").pathname,
    );
    const relative = pathname.replace(/^\/miniapp\/?/, "") || "index.html";
    const file = resolve(frontend, relative);
    if (
      !file.startsWith(`${frontend}${sep}`) &&
      file !== resolve(frontend, "index.html")
    )
      return response.writeHead(403).end();
    try {
      response.writeHead(200, {
        "Content-Type": mime[extname(file)] || "application/octet-stream",
      });
      response.end(await readFile(file));
    } catch {
      response.writeHead(404).end("Not found");
    }
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));

  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      permissions: ["clipboard-read", "clipboard-write"],
    });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.addInitScript(() => {
      const callbacks = {};
      const NativeEventSource = window.EventSource;
      window.EventSource = class extends NativeEventSource {
        constructor(...args) {
          super(...args);
          window.__adminEvents = this;
        }
      };
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
          onEvent(name, callback) {
            (callbacks[name] ??= []).push(callback);
          },
          __callbacks: callbacks,
        },
      };
    });
    await page.route("https://telegram.org/**", (route) => route.abort());
    await page.route("**/api/**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname;
      const method = req.method();
      requestLog.push({
        path,
        method,
        search: url.searchParams.get("search") || "",
      });
      if (method === "POST" && path.endsWith("/send-selected")) {
        sendCalls += 1;
        sentUserIds.push(req.postDataJSON().user_ids);
      }
      if (method === "POST" && path.endsWith("/mark-paid")) markPaidCalls += 1;
      const requestQueue = delayedRequests.get(path);
      const delayedRequest = requestQueue?.shift();
      if (requestQueue && !requestQueue.length) delayedRequests.delete(path);
      if (delayedRequest) {
        const result = await delayedRequest;
        if (result?.status) {
          return route.fulfill({
            status: result.status,
            contentType: "application/json",
            body: JSON.stringify({ detail: result.detail || "Request failed" }),
          });
        }
      }
      let data = {};
      if (path.endsWith("/events-token")) data = { token: "browser-test" };
      else if (path.endsWith("/events"))
        return route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: ": connected\n\n",
        });
      else if (path.endsWith("/admin/users")) {
        const search = url.searchParams.get("search") || "";
        if (search === "empty")
          data = { items: [], offset: 0, has_more: false };
        else if (search === "load-error")
          return route.fulfill({
            status: 503,
            contentType: "application/json",
            body: JSON.stringify({ detail: "Search unavailable" }),
          });
        else if (search)
          data = {
            items: [{ id: 901, full_name: "Найденный пользователь" }],
            offset: 0,
            has_more: false,
          };
        else {
          const offset = Number(url.searchParams.get("offset") || 0);
          const items = Array.from(
            { length: Math.min(50, 120 - offset) },
            (_, index) => ({
              id: offset + index + 1,
              full_name: `${renamedUserId === offset + index + 1 ? "Новое ФИО" : "Пользователь"} ${offset + index + 1} с длинной фамилией для плотного списка`,
            }),
          );
          data = { items, offset, has_more: offset + items.length < 120 };
          users = [...users.slice(0, offset), ...items];
        }
      } else if (path.endsWith("/admin/payouts") && method === "GET")
        data = payouts;
      else if (path.endsWith("/admin/payouts") && method === "POST") {
        const body = req.postDataJSON();
        const payoutId = createPayoutCalls++ === 0 ? 39 : 41;
        data = {
          id: payoutId,
          status: "draft",
          period_start_day: body.period_start_day,
          period_start_month: body.period_start_month,
          period_end_day: body.period_end_day,
          period_end_month: body.period_end_month,
          period_label: `${String(body.period_start_day).padStart(2, "0")}.${String(body.period_start_month).padStart(2, "0")} – ${String(body.period_end_day).padStart(2, "0")}.${String(body.period_end_month).padStart(2, "0")}`,
        };
        payouts.unshift(data);
      } else if (/\/admin\/payouts\/\d+$/.test(path)) {
        const id = Number(path.split("/").at(-1));
        if (failedPayoutDetails.has(id))
          return route.fulfill({
            status: 503,
            contentType: "application/json",
            body: JSON.stringify({ detail: "Payout unavailable" }),
          });
        data = { payout: payouts.find((payout) => payout.id === id) || null };
      } else if (path.endsWith("/recipients") && method === "GET") {
        const payoutId = Number(path.split("/").at(-2));
        data = payoutId === 38 ? historicalRecipients : payoutId === 39 ? sentRecipients : recipients;
      } else if (path.endsWith("/send-selected")) {
        await new Promise((done) => setTimeout(done, 100));
        const payout = payouts.find(
          (item) => item.id === Number(path.split("/").at(-2)),
        );
        payout.status = "sent";
        sentRecipients = req.postDataJSON().user_ids.map((userId, index) => ({
          id: 600 + index,
          user_id: userId,
          full_name:
            users.find((user) => user.id === userId)?.full_name ||
            `Получатель ${userId}`,
          status: "sent",
        }));
        data = { payout, recipients: sentRecipients };
      } else if (path.endsWith("/mark-paid")) {
        const id = Number(path.split("/").at(-2));
        historicalRecipients = historicalRecipients.map((recipient) =>
          recipient.id === id ? { ...recipient, status: "paid" } : recipient,
        );
        return route.fulfill({ status: 204, body: "" });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    });

    await page.goto(`http://127.0.0.1:${server.address().port}/miniapp/`);
    await page
      .locator('[data-screen="composer"]')
      .waitFor({ state: "visible" });
    assert.equal(await page.locator("[data-mobile-view]").count(), 4);
    assert.deepEqual(
      (await page.locator("[data-mobile-view]").allTextContents()).map(
        (label) => label.trim(),
      ),
      ["Создать", "Выбор", "Получатели", "История"],
    );

    await page.locator('[data-mobile-view="users"]').click();
    await page.locator('[data-action="load-more-users"]').click();
    await page.locator('input[data-user-id="100"]').waitFor();
    for (const [width, height] of [
      [320, 640],
      [360, 780],
      [390, 844],
      [768, 1024],
      [1024, 768],
      [1440, 900],
    ]) {
      await page.setViewportSize({ width, height });
      await page.evaluate(() => window.scrollTo(0, 0));
      const navBounds = await page.locator(".bottom-nav").evaluate((nav) => {
        const rect = nav.getBoundingClientRect();
        return {
          top: rect.top,
          bottom: rect.bottom,
          headerBottom: document.querySelector(".app-header").getBoundingClientRect().bottom,
          workspaceTop: document.querySelector(".workspace").getBoundingClientRect().top,
          scrollY,
        };
      });
      assert.equal(navBounds.scrollY, 0);
      assert.ok(navBounds.top >= 0 && navBounds.bottom <= height, `navigation outside viewport at ${width}x${height}: ${JSON.stringify(navBounds)}`);
      if (width >= 760) {
        assert.ok(navBounds.top >= navBounds.headerBottom - 1, `navigation must follow header at ${width}px`);
        assert.ok(navBounds.bottom <= navBounds.workspaceTop + 1, `navigation must precede workspace at ${width}px`);
      }
      for (const tab of ["composer", "users", "recipients", "payouts"]) {
        await page.locator(`[data-mobile-view="${tab}"]`).click();
        assert.equal(
          await page.locator(`[data-screen="${tab}"]`).isVisible(),
          true,
        );
        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
          true,
          `horizontal overflow at ${width}px in ${tab}`,
        );
        assert.deepEqual(
          await page.evaluate(() => ({
            visibleHeadings: [...document.querySelectorAll("h1, .screen-heading")]
              .filter((heading) => heading.getClientRects().length)
              .map((heading) => heading.textContent.trim()),
            titleId: document.querySelector(`[data-screen="${document.querySelector(".nav-button.active").dataset.mobileView}"]`).getAttribute("aria-labelledby"),
          })),
          {
            visibleHeadings: [
              (await page.locator("#screen-title").innerText()).trim(),
            ],
            titleId: "screen-title",
          },
        );
      }
    }

    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('[data-mobile-view="composer"]').click();
    await page.locator("#period-start").fill("31.04");
    await page.locator('[data-action="create-payout"]').click();
    assert.match(
      await page.locator("#payout-validation").innerText(),
      /Проверьте даты/,
    );
    await page.locator("#period-start").fill("01.11");
    await page.locator("#period-end").fill("07.11");
    const editor = page.locator("#message-before");
    await editor.fill("A🙂B");
    await editor.press("Control+A");
    await editor.pressSequentially("🙂x");
    await editor.press("Control+Z");
    await editor.press("Control+Z");
    assert.equal(await editor.inputValue(), "A🙂B");
    await editor.press("Control+Y");
    await editor.press("Control+Y");
    assert.equal(await editor.inputValue(), "🙂x");
    await page.evaluate(() => navigator.clipboard.writeText("🙂".repeat(4000)));
    await editor.press("Control+A");
    await editor.press("Control+V");
    assert.equal(
      Number(
        (await page.locator("#message-count").innerText()).split(" / ")[0],
      ),
      3500,
    );
    await editor.evaluate((element) => {
      element.dispatchEvent(
        new CompositionEvent("compositionstart", { bubbles: true }),
      );
      element.value = "Ω".repeat(4000);
      element.dispatchEvent(
        new InputEvent("input", {
          bubbles: true,
          inputType: "insertCompositionText",
          data: "Ω",
          isComposing: true,
        }),
      );
      element.dispatchEvent(
        new CompositionEvent("compositionend", { bubbles: true, data: "Ω" }),
      );
    });
    await page.waitForTimeout(20);
    assert.ok(
      Number(
        (await page.locator("#message-count").innerText()).split(" / ")[0],
      ) <= 3500,
    );
    await editor.press("End");
    await editor.pressSequentially("x");
    assert.ok(
      Number(
        (await page.locator("#message-count").innerText()).split(" / ")[0],
      ) <= 3500,
    );
    await editor.press("Backspace");
    assert.ok(
      Number(
        (await page.locator("#message-count").innerText()).split(" / ")[0],
      ) < 3500,
    );
    await editor.fill("Длинный текст 🙂\n".repeat(50));
    await page.locator('[data-action="toggle-preview"]').click();
    await page.locator("#period-end").fill("08.11");
    assert.match(await editor.inputValue(), /Длинный текст/);
    await page.locator("#period-end").fill("07.11");
    await page.locator('[data-action="create-payout"]').click();
    await page.locator("#create-success").waitFor({ state: "visible" });
    assert.match(
      await page.locator("#create-success").innerText(),
      /Выплата #39 создана/,
    );
    await page.waitForTimeout(30);
    const payout39RequestsBeforeGo = requestLog.filter(
      ({ path, method }) =>
        (path === "/api/admin/payouts/39" ||
          path === "/api/admin/payouts/39/recipients") &&
        method === "GET",
    ).length;
    await page.locator('[data-action="go-selection"]').click();
    await page.waitForFunction(
      () =>
        document.querySelector(".nav-button.active")?.dataset.mobileView ===
        "users",
    );

    assert.equal(
      await page
        .locator('#create-success [data-action="go-selection"]')
        .getAttribute("data-payout-id"),
      "39",
    );
    assert.deepEqual(
      await page.evaluate(async () => {
        const { state } = await import("/miniapp/js/store.js");
        return [
          Number(state.selectedPayoutId),
          Number(state.selectedPayoutDetail?.payout?.id || 0),
        ];
      }),
      [39, 39],
    );
    assert.equal(
      requestLog.filter(
        ({ path, method }) =>
          (path === "/api/admin/payouts/39" ||
            path === "/api/admin/payouts/39/recipients") &&
          method === "GET",
      ).length,
      payout39RequestsBeforeGo,
    );

    const initiallyDisabledSend = page.locator('[data-action="send-payout"]');
    assert.equal(await initiallyDisabledSend.isDisabled(), true);
    await initiallyDisabledSend.evaluate((button) =>
      button.dispatchEvent(new MouseEvent("click", { bubbles: true })),
    );
    assert.equal(await page.locator("#confirm-dialog").isVisible(), false);

    await page.locator("#users-search").fill("ФИО сохраняется");
    await page.waitForTimeout(350);
    assert.equal(await page.locator("[data-user-row]").count(), 1);
    await page.locator('[data-mobile-view="payouts"]').click();
    await page.locator('[data-mobile-view="users"]').click();
    assert.equal(
      await page.locator("#users-search").inputValue(),
      "ФИО сохраняется",
    );
    await page.locator("#users-search").fill("empty");
    await page.waitForTimeout(350);
    assert.match(await page.locator("#users-state").innerText(), /не найдены/);
    await page.locator("#users-search").fill("load-error");
    await page.waitForTimeout(350);
    assert.match(
      await page.locator("#users-state").innerText(),
      /Не удалось загрузить пользователей/,
    );
    await page.locator("#users-search").fill("");
    await page.waitForTimeout(350);
    assert.ok(
      requestLog.some(
        ({ path, search }) =>
          path.endsWith("/admin/users") && search === "ФИО сохраняется",
      ),
    );
    await page.locator('[data-action="load-more-users"]').click();
    await page.locator("input[data-user-id='100']").waitFor();
    assert.equal(await page.locator("input[data-user-id]").count(), 100);
    await page.locator('input[data-user-id="1"]').check();
    await page.locator('input[data-user-id="2"]').check();
    await page.locator('[data-mobile-view="payouts"]').click();
    await page.locator('.payout-row[data-payout-id="40"]').click();
    await page.locator('[data-mobile-view="users"]').click();
    assert.equal(await page.locator("#selected-count").innerText(), "0 получателей");
    await page.locator('input[data-user-id="2"]').check();
    await page.locator('input[data-user-id="3"]').check();
    const assertVisibleSelection = async (expected) => {
      const observed = await page.locator("input[data-user-id]").evaluateAll((inputs) =>
        inputs.filter((input) => input.checked).map((input) => Number(input.dataset.userId)),
      );
      assert.deepEqual(observed, expected);
      const classes = await page.locator("[data-user-row]").evaluateAll((rows) =>
        rows.filter((row) => row.classList.contains("selected")).map((row) => Number(row.dataset.userRow)),
      );
      assert.deepEqual(classes, expected);
      assert.equal(await page.locator("#selected-count").innerText(), `${expected.length} ${expected.length === 1 ? "получатель" : "получателя"}`);
    };
    await assertVisibleSelection([2, 3]);
    await page.locator('[data-mobile-view="payouts"]').click();
    await page.locator('.payout-row[data-payout-id="39"]').click();
    await page.locator('[data-mobile-view="users"]').click();
    await assertVisibleSelection([1, 2]);
    const selectedCheckbox = page.locator('input[data-user-id="1"]');
    renamedUserId = 1;
    await page.evaluate(() =>
      window.__adminEvents.dispatchEvent(
        new MessageEvent("users_changed", { data: "{}" }),
      ),
    );
    await page.waitForFunction(() =>
      document.querySelector('[data-user-row="1"] .user-name')?.textContent.startsWith("Новое ФИО"),
    );
    assert.equal(await selectedCheckbox.isChecked(), true);
    assert.equal(await selectedCheckbox.evaluate((checkbox) => checkbox === document.querySelector('input[data-user-id="1"]')), true);
    await page.locator('[data-mobile-view="payouts"]').click();
    await page.locator('.payout-row[data-payout-id="40"]').click();
    await page.locator('[data-mobile-view="users"]').click();
    await assertVisibleSelection([2, 3]);
    await page.locator('[data-mobile-view="payouts"]').click();
    await page.locator('.payout-row[data-payout-id="39"]').click();
    await page.locator('[data-mobile-view="users"]').click();
    await assertVisibleSelection([1, 2]);

    await page.setViewportSize({ width: 390, height: 420 });
    await page.locator("#users-search").focus();
    const keyboardLayout = await page.evaluate(() => ({
      navTop: document.querySelector(".bottom-nav").getBoundingClientRect().top,
      barBottom: document
        .querySelector("#selection-bar")
        .getBoundingClientRect().bottom,
      searchBottom: document
        .querySelector("#users-search")
        .getBoundingClientRect().bottom,
    }));
    assert.ok(keyboardLayout.barBottom <= keyboardLayout.navTop);
    assert.ok(keyboardLayout.searchBottom < keyboardLayout.navTop);
    await page.setViewportSize({ width: 390, height: 844 });

    await page.locator('[data-mobile-view="payouts"]').click();
    await page.locator('.payout-row[data-payout-id="37"]').click();
    await page.locator('[data-mobile-view="users"]').click();
    assert.equal(
      await page.locator("input[data-user-id]").first().isEnabled(),
      false,
    );
    assert.equal(
      await page.locator("#selected-count").innerText(),
      "0 получателей",
    );
    await page.locator('[data-mobile-view="payouts"]').click();
    await page.locator('.payout-row[data-payout-id="39"]').click();
    await page.locator('[data-mobile-view="users"]').click();
    assert.equal(
      await page.locator('input[data-user-id="1"]').isChecked(),
      true,
    );

    const sendButton = page.locator('[data-action="send-payout"]');
    await sendButton.click();
    const sendDialog = page.locator("#confirm-dialog");
    assert.equal(await sendDialog.isVisible(), true);
    assert.match(
      await page.locator("#dialog-content").innerText(),
      /Выплата #39[\s\S]*Получателей: 2/,
    );
    assert.match(
      await page.locator("#dialog-warning").innerText(),
      /реальные сообщения/,
    );
    assert.equal(
      await page
        .locator("[data-dialog-cancel]")
        .first()
        .evaluate((element) => element === document.activeElement),
      true,
    );
    await page.keyboard.press("Escape");
    assert.equal(await sendDialog.isVisible(), false);
    assert.equal(
      await sendButton.evaluate(
        (element) => element === document.activeElement,
      ),
      true,
    );
    await sendButton.click();
    await page.locator("#dialog-confirm").dblclick();
    await page.waitForTimeout(200);
    assert.equal(sendCalls, 1);
    assert.deepEqual(sentUserIds[0], [1, 2]);

    await page.locator('[data-mobile-view="payouts"]').click();
    await page.locator('.payout-row[data-payout-id="38"]').click();
    await page.locator('[data-mobile-view="recipients"]').click();
    await page.locator("[data-expand-recipient='501']").click();
    assert.equal(await page.locator("[data-expand-recipient='501']").getAttribute("aria-expanded"), "true", await page.locator("[data-recipient-row='501']").evaluate((node) => node.outerHTML));
    await page.locator("[data-filter='problems']").click();
    assert.equal(await page.locator("[data-recipient-row]").count(), 1);
    await page.locator("[data-filter='all']").click();
    assert.equal(await page.locator("[data-expand-recipient='501']").getAttribute("aria-expanded"), "true");
    assert.equal(await page.locator("[data-recipient-details='501']").evaluate((node) => node.classList.contains("hidden")), false, await page.locator("[data-recipient-row='501']").evaluate((node) => node.outerHTML));
    const retainedNodes = await page.evaluate(() => {
      window.__recipientNode501 = document.querySelector('[data-recipient-row="501"]');
      window.__recipientNode502 = document.querySelector('[data-recipient-row="502"]');
      window.__recipientNode503 = document.querySelector('[data-recipient-row="503"]');
      window.__recipientOrderBefore = [...document.querySelectorAll("[data-recipient-row]")].map((row) => row.dataset.recipientRow);
      return true;
    });
    assert.equal(retainedNodes, true);
    await page.locator('[data-copy-recipient="501"]').focus();
    assert.match(
      await page.locator("[data-recipient-details='501'] pre").innerText(),
      /^Сбербанк/,
    );
    await page.locator("[data-copy-recipient='501']").click();
    await page.waitForFunction(async () =>
      (await navigator.clipboard.readText()).startsWith("Сбербанк"),
    );
    assert.match(
      await page.evaluate(() => navigator.clipboard.readText()),
      /^Сбербанк/,
    );
    await page.evaluate(() => window.scrollTo(0, 80));
    historicalRecipients = historicalRecipients
      .filter((recipient) => recipient.id !== 729)
      .map((recipient) =>
        recipient.id === 502 ? { ...recipient, status: "paid" } : recipient,
      );
    await page.evaluate(() =>
      window.__adminEvents.dispatchEvent(
        new MessageEvent("payout_recipients_changed", {
          data: JSON.stringify({ payout_id: 38 }),
        }),
      ),
    );
    await page.waitForFunction(
      () =>
        document.querySelector('[data-recipient-row="502"] .badge')
          ?.textContent === "Выплачено",
    );
    assert.equal(
      await page.locator("[data-filter='all']").getAttribute("aria-pressed"),
      "true",
    );
    assert.equal(
      await page.evaluate(() => document.querySelector('[data-recipient-row="501"]') === window.__recipientNode501),
      true,
    );
    assert.equal(
      await page.evaluate(() => document.querySelector('[data-recipient-row="503"]') === window.__recipientNode503),
      true,
    );
    assert.equal(
      await page.evaluate(() => document.querySelector('[data-recipient-row="502"]') === window.__recipientNode502),
      true,
    );
    assert.equal(await page.locator('[data-recipient-row="729"]').count(), 0);
    assert.deepEqual(
      await page.locator("#recipient-summary .stat strong").allTextContents(),
      ["32", "1", "2", "0"],
    );
    assert.ok(
      (await page.locator("[data-recipient-row]").evaluateAll((rows) => rows.map((row) => row.dataset.recipientRow).indexOf("502"))) >
        (await page.evaluate(() => window.__recipientOrderBefore.indexOf("502"))),
    );
    assert.equal(
      await page.locator('[data-copy-recipient="501"]').evaluate((element) => element === document.activeElement),
      true,
    );
    assert.equal(
      await page
        .locator("[data-expand-recipient='501']")
        .getAttribute("aria-expanded"),
      "true",
    );
    assert.ok(Math.abs((await page.evaluate(() => window.scrollY)) - 80) < 4);

    await page.locator('[data-action="mark-paid"]').click();
    assert.equal(
      await page.locator("#dialog-title").innerText(),
      "Подтвердить выплату?",
    );
    assert.match(
      await page.locator("#dialog-content").innerText(),
      /Александр Фамилия[\s\S]*#38 · 01\.11 – 07\.11/,
    );
    await page.locator("#dialog-confirm").click();
    await page.waitForFunction(
      () =>
        document.querySelector('[data-recipient-row="501"] .badge')
          ?.textContent === "Выплачено",
    );
    assert.equal(markPaidCalls, 1);

    const selectPayout = async (id) => {
      const requestStarted = page.waitForRequest(
        (request) =>
          new URL(request.url()).pathname === `/api/admin/payouts/${id}`,
      );
      await page.locator('[data-mobile-view="payouts"]').click();
      await page.locator(`.payout-row[data-payout-id="${id}"]`).click();
      await requestStarted;
    };
    const waitForPayoutContext = (id) =>
      page.waitForFunction(
        (payoutId) =>
          document
            .querySelector("#recipients-context")
            ?.textContent.includes(`Текущая: #${payoutId} ·`),
        id,
      );
    const readPayoutState = () =>
      page.evaluate(async () => {
        const { state } = await import("/miniapp/js/store.js");
        return {
          selectedPayoutId: Number(state.selectedPayoutId),
          detailPayoutId: Number(state.selectedPayoutDetail?.payout?.id || 0),
          recipientIds: state.recipients.map((recipient) => Number(recipient.id)),
          selectedUsers: [...state.selectedUsers].sort((a, b) => a - b),
          selectedUsersByPayout40: [
            ...(state.selectedUsersByPayout.get(40) || new Set()),
          ].sort((a, b) => a - b),
          selectedUsersByPayout41: [
            ...(state.selectedUsersByPayout.get(41) || new Set()),
          ].sort((a, b) => a - b),
          activeMobileView: state.activeMobileView,
          visibleUserChecks: [...document.querySelectorAll("input[data-user-id]")]
            .filter((checkbox) => checkbox.checked)
            .map((checkbox) => Number(checkbox.dataset.userId))
            .sort((a, b) => a - b),
          visibleSelectedRows: [...document.querySelectorAll("[data-user-row]")]
            .filter((row) => row.classList.contains("selected"))
            .map((row) => Number(row.dataset.userRow))
            .sort((a, b) => a - b),
          markPaidErrorIds: Object.keys(state.recipientOperationError).map(Number),
          markPaidLoading: state.loading.markPaid,
          loadingRecipientId: state.loadingRecipientId,
        };
      });

    // A stale failure after a successful selection must not replace current state or toast.
    const staleFailure = delayNextRequest("/api/admin/payouts/38");
    await selectPayout(38);
    await selectPayout(40);
    await waitForPayoutContext(40);
    const toastBeforeStaleFailure = await page.locator("#toast").innerText();
    const staleFailureResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/admin/payouts/38",
    );
    staleFailure.release({ status: 503, detail: "stale failure" });
    await staleFailureResponse;
    await page.waitForTimeout(30);
    assert.match(await page.locator("#recipients-context").innerText(), /Текущая: #40/);
    assert.equal(await page.locator("#toast").innerText(), toastBeforeStaleFailure);
    assert.doesNotMatch(await page.locator("#toast").getAttribute("class"), /toast-error/);

    // A stale success must stop before requesting recipients or updating the current payout.
    const staleSuccess = delayNextRequest("/api/admin/payouts/40");
    const recipients40Before = requestLog.filter(
      ({ path }) => path === "/api/admin/payouts/40/recipients",
    ).length;
    await selectPayout(40);
    await selectPayout(38);
    await waitForPayoutContext(38);
    const staleSuccessResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/admin/payouts/40",
    );
    staleSuccess.release();
    await staleSuccessResponse;
    await page.waitForTimeout(30);
    assert.match(await page.locator("#recipients-context").innerText(), /Текущая: #38/);
    assert.equal(
      requestLog.filter(({ path }) => path === "/api/admin/payouts/40/recipients").length,
      recipients40Before,
    );

    // The previous request finishing while the new request is pending cannot clear its loading state.
    const oldWhileNewLoading = delayNextRequest("/api/admin/payouts/38");
    await selectPayout(38);
    const newPayoutLoading = delayNextRequest("/api/admin/payouts/40");
    await selectPayout(40);
    const oldLoadingResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/admin/payouts/38",
    );
    oldWhileNewLoading.release({ status: 503, detail: "old request failed" });
    await oldLoadingResponse;
    assert.match(await page.locator("#recipients-state").innerText(), /Загрузка получателей/);
    const newLoadingResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/admin/payouts/40",
    );
    newPayoutLoading.release();
    await newLoadingResponse;
    await waitForPayoutContext(40);

    // Errors belonging to the selected payout remain visible.
    const currentFailure = delayNextRequest("/api/admin/payouts/40");
    await selectPayout(40);
    const currentFailureResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/admin/payouts/40",
    );
    currentFailure.release({ status: 503, detail: "current payout unavailable" });
    await currentFailureResponse;
    await page.waitForFunction(() =>
      document.querySelector("#toast")?.classList.contains("toast-error"),
    );
    assert.match(await page.locator("#toast").innerText(), /Backend временно недоступен/);
    assert.match(await page.locator("#recipients-state").innerText(), /Backend временно недоступен/);

    // SSE refreshes the selected payout, while an older payout response is still pending.
    const staleForSse = delayNextRequest("/api/admin/payouts/38");
    await selectPayout(38);
    await selectPayout(40);
    await waitForPayoutContext(40);
    recipients = [
      { id: 940, user_id: 9, full_name: "SSE актуальный получатель", status: "pending" },
    ];
    await page.evaluate(() =>
      window.__adminEvents.dispatchEvent(
        new MessageEvent("payout_recipients_changed", {
          data: JSON.stringify({ payout_id: 40 }),
        }),
      ),
    );
    await page.waitForFunction(() =>
      [...document.querySelectorAll("[data-recipient-row]")].some((row) =>
        row.textContent.includes("SSE актуальный получатель"),
      ),
    );
    const staleForSseResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/admin/payouts/38",
    );
    staleForSse.release({ status: 503, detail: "stale SSE race" });
    await staleForSseResponse;
    await page.waitForTimeout(30);
    assert.match(await page.locator("#recipients-context").innerText(), /Текущая: #40/);
    assert.equal(await page.locator('[data-recipient-row="940"]').count(), 1);
    assert.equal(await page.locator("#recipients-state").innerText(), "");

    // Failed send for #39 reports its own payout and leaves #40's selection intact.
    payouts.find((payout) => payout.id === 39).status = "draft";
    await selectPayout(39);
    await page.locator('[data-mobile-view="users"]').click();
    await page.locator('input[data-user-id="1"]').check();
    await page.locator('input[data-user-id="2"]').check();
    const failedSend = delayNextRequest("/api/admin/payouts/39/send-selected");
    const failedSendStarted = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        new URL(request.url()).pathname === "/api/admin/payouts/39/send-selected",
    );
    await page.locator('[data-action="send-payout"]').click();
    await page.locator("#dialog-confirm").click();
    await failedSendStarted;
    await page.keyboard.press("Escape");
    await selectPayout(40);
    await waitForPayoutContext(40);
    await page.locator('[data-mobile-view="users"]').click();
    await assertVisibleSelection([2, 3]);
    const failedSendResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/admin/payouts/39/send-selected",
    );
    failedSend.release({ status: 503, detail: "send failed" });
    await failedSendResponse;
    await page.waitForFunction(() =>
      document.querySelector("#toast")?.textContent.includes("выплаты #39"),
    );
    assert.match(await page.locator("#toast").innerText(), /#39/);
    assert.match(await page.locator("#recipients-context").innerText(), /Текущая: #40/);
    assert.equal(await page.locator('[data-recipient-row="940"]').count(), 1);
    assert.deepEqual((await readPayoutState()).selectedUsers, [2, 3]);
    assert.equal((await readPayoutState()).selectedPayoutId, 40);
    assert.equal((await readPayoutState()).detailPayoutId, 40);
    assert.deepEqual((await readPayoutState()).recipientIds, [940]);
    assert.equal(sendCalls, 2);

    // A late successful send updates #39's history but not #40's recipients or selection.
    await selectPayout(39);
    await page.locator('[data-mobile-view="users"]').click();
    await assertVisibleSelection([1, 2]);
    const successfulSend = delayNextRequest("/api/admin/payouts/39/send-selected");
    const successfulSendStarted = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        new URL(request.url()).pathname === "/api/admin/payouts/39/send-selected",
    );
    await page.locator('[data-action="send-payout"]').click();
    await page.locator("#dialog-confirm").click();
    await successfulSendStarted;
    await page.keyboard.press("Escape");
    await selectPayout(40);
    await waitForPayoutContext(40);
    await page.locator('[data-mobile-view="users"]').click();
    await assertVisibleSelection([2, 3]);
    const successfulSendResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/admin/payouts/39/send-selected",
    );
    successfulSend.release();
    await successfulSendResponse;
    await page.waitForFunction(() =>
      document.querySelector("#toast")?.textContent.includes("для выплаты #39 запущена"),
    );
    assert.match(await page.locator("#recipients-context").innerText(), /Текущая: #40/);
    assert.equal(await page.locator('[data-recipient-row="940"]').count(), 1);
    assert.deepEqual(await page.locator("#recipient-summary .stat strong").allTextContents(), ["1", "0", "0", "0"]);
    assert.deepEqual((await readPayoutState()).selectedUsers, [2, 3]);
    assert.deepEqual((await readPayoutState()).selectedUsersByPayout40, [2, 3]);
    assert.deepEqual((await readPayoutState()).recipientIds, [940]);
    assert.equal((await readPayoutState()).detailPayoutId, 40);
    assert.equal(payouts.find((payout) => payout.id === 39).status, "sent");
    assert.equal(sendCalls, 3);
    assert.deepEqual(sentUserIds.slice(-2), [[1, 2], [1, 2]]);

    // Late payment success for #38 keeps #40 visible and does not optimistically change status.
    historicalRecipients = historicalRecipients.map((recipient) =>
      recipient.id === 501
        ? { ...recipient, status: "payment_received" }
        : recipient,
    );
    await selectPayout(38);
    await page.locator('[data-mobile-view="recipients"]').click();
    await page.locator("[data-expand-recipient='501']").click();
    await page.locator('[data-action="mark-paid"]').click();
    const latePaymentSuccess = delayNextRequest(
      "/api/admin/payouts/38/recipients/501/mark-paid",
    );
    const paymentSuccessStarted = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        new URL(request.url()).pathname ===
          "/api/admin/payouts/38/recipients/501/mark-paid",
    );
    await page.locator("#dialog-confirm").click();
    await paymentSuccessStarted;
    assert.equal(
      await page.locator('[data-recipient-row="501"] .badge').innerText(),
      "Данные подтверждены · к оплате",
    );
    await page.keyboard.press("Escape");
    await selectPayout(40);
    await waitForPayoutContext(40);
    const paymentSuccessResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname ===
          "/api/admin/payouts/38/recipients/501/mark-paid",
    );
    latePaymentSuccess.release();
    await paymentSuccessResponse;
    await page.waitForFunction(() =>
      document.querySelector("#toast")?.textContent.includes("в выплате #38 отмечена"),
    );
    assert.match(await page.locator("#recipients-context").innerText(), /Текущая: #40/);
    assert.deepEqual((await readPayoutState()).recipientIds, [940]);
    assert.deepEqual((await readPayoutState()).selectedUsers, [2, 3]);
    assert.equal((await readPayoutState()).detailPayoutId, 40);
    assert.equal(historicalRecipients.find((recipient) => recipient.id === 501).status, "paid");
    assert.equal((await readPayoutState()).markPaidErrorIds.includes(501), false);

    // A late payment error is attributed to #38 without adding an error to #40's recipient state.
    historicalRecipients = historicalRecipients.map((recipient) =>
      recipient.id === 501
        ? { ...recipient, status: "payment_received" }
        : recipient,
    );
    await selectPayout(38);
    await page.locator('[data-mobile-view="recipients"]').click();
    await page.locator("[data-expand-recipient='501']").click();
    await page.locator('[data-action="mark-paid"]').click();
    const latePaymentFailure = delayNextRequest(
      "/api/admin/payouts/38/recipients/501/mark-paid",
    );
    const paymentFailureStarted = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        new URL(request.url()).pathname ===
          "/api/admin/payouts/38/recipients/501/mark-paid",
    );
    await page.locator("#dialog-confirm").click();
    await paymentFailureStarted;
    await page.keyboard.press("Escape");
    await selectPayout(40);
    await waitForPayoutContext(40);
    const paymentFailureResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname ===
          "/api/admin/payouts/38/recipients/501/mark-paid",
    );
    latePaymentFailure.release({ status: 503, detail: "payment failed" });
    await paymentFailureResponse;
    await page.waitForFunction(() =>
      document.querySelector("#toast")?.textContent.includes("Не удалось отметить оплату в выплате #38"),
    );
    assert.doesNotMatch(await page.locator("#toast").innerText(), /выплате #40/);
    assert.deepEqual((await readPayoutState()).recipientIds, [940]);
    assert.deepEqual((await readPayoutState()).selectedUsers, [2, 3]);
    assert.equal((await readPayoutState()).detailPayoutId, 40);
    assert.equal((await readPayoutState()).markPaidErrorIds.includes(501), false);
    assert.equal((await readPayoutState()).markPaidLoading, false);
    assert.equal((await readPayoutState()).loadingRecipientId, null);
    assert.equal(markPaidCalls, 3);

    // An old #38 refresh cannot occupy the scheduler after a quick #38 → #40 → #38 switch.
    await selectPayout(38);
    await waitForPayoutContext(38);
    const oldScheduled38 = delayNextRequest("/api/admin/payouts/38");
    const oldRequestStarted = page.waitForRequest(
      (request) =>
        new URL(request.url()).pathname === "/api/admin/payouts/38",
    );
    await page.evaluate(() =>
      window.__adminEvents.dispatchEvent(
        new MessageEvent("payout_recipients_changed", {
          data: JSON.stringify({ payout_id: 38 }),
        }),
      ),
    );
    await oldRequestStarted;
    await selectPayout(40);
    await selectPayout(38);
    await waitForPayoutContext(38);
    const detailRequestsBeforeSse = requestLog.filter(
      ({ path, method }) => path === "/api/admin/payouts/38" && method === "GET",
    ).length;
    historicalRecipients = historicalRecipients.map((recipient) =>
      recipient.id === 501
        ? { ...recipient, full_name: "Получатель обновлён по SSE", status: "paid" }
        : recipient,
    );
    const sseRefresh = delayNextRequest("/api/admin/payouts/38");
    const firstSseRefresh = page.waitForRequest(
      (request) =>
        new URL(request.url()).pathname === "/api/admin/payouts/38",
    );
    await page.evaluate(() =>
      window.__adminEvents.dispatchEvent(
        new MessageEvent("payout_recipients_changed", {
          data: JSON.stringify({ payout_id: 38 }),
        }),
      ),
    );
    await firstSseRefresh;
    const secondSseRefresh = page.waitForRequest(
      (request) =>
        new URL(request.url()).pathname === "/api/admin/payouts/38",
    );
    await page.evaluate(() =>
      window.__adminEvents.dispatchEvent(
        new MessageEvent("payout_recipients_changed", {
          data: JSON.stringify({ payout_id: 38 }),
        }),
      ),
    );
    const sseRefreshResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/admin/payouts/38",
    );
    sseRefresh.release();
    await sseRefreshResponse;
    await secondSseRefresh;
    await page.waitForFunction(() =>
      document.querySelector('[data-recipient-row="501"] .recipient-name')
        ?.textContent.includes("Получатель обновлён по SSE"),
    );
    const oldRefreshResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/admin/payouts/38",
    );
    oldScheduled38.release({ status: 503, detail: "obsolete refresh" });
    await oldRefreshResponse;
    assert.ok(
      requestLog.filter(
        ({ path, method }) => path === "/api/admin/payouts/38" && method === "GET",
      ).length >= detailRequestsBeforeSse + 2,
    );
    assert.equal(await page.locator("#recipients-state").innerText(), "");
    assert.match(await page.locator('[data-recipient-row="501"] .badge').innerText(), /Выплачено/);
    assert.deepEqual((await readPayoutState()).recipientIds[0], 501);
    assert.equal(sendCalls, 3);
    assert.equal(markPaidCalls, 3);

    // A late create response adds history without replacing the payout selected meanwhile.
    await page.locator('[data-mobile-view="composer"]').click();
    await page.locator("#period-start").fill("15.11");
    await page.locator("#period-end").fill("21.11");
    const delayedCreate = delayNextRequest("/api/admin/payouts");
    const createStarted = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        new URL(request.url()).pathname === "/api/admin/payouts",
    );
    await page.locator('[data-action="create-payout"]').click();
    await createStarted;
    await selectPayout(40);
    await waitForPayoutContext(40);
    const createResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/admin/payouts",
    );
    delayedCreate.release();
    await createResponse;
    await page.waitForFunction(() =>
      document.querySelector("#toast")?.textContent.includes("Выплата #41 создана"),
    );
    assert.match(await page.locator("#recipients-context").innerText(), /Текущая: #40/);
    assert.deepEqual((await readPayoutState()).selectedUsers, [2, 3]);
    assert.equal((await readPayoutState()).selectedPayoutId, 40);
    assert.equal((await readPayoutState()).detailPayoutId, 40);
    assert.equal(payouts.some((payout) => payout.id === 41), true);
    assert.equal(
      requestLog.filter(
        ({ path, method }) => path === "/api/admin/payouts" && method === "POST",
      ).length,
      2,
    );

    // The created payout CTA selects its explicit target, and a failed or repeated
    // activation never opens the current payout under the created payout's label.
    await page.locator('[data-mobile-view="composer"]').click();
    const createdPayoutAction = page.locator(
      '#create-success [data-action="go-selection"]',
    );
    assert.equal(await createdPayoutAction.getAttribute("data-payout-id"), "41");
    failedPayoutDetails.add(41);
    const detailRequestsBeforeFailure = requestLog.filter(
      ({ path, method }) => path === "/api/admin/payouts/41" && method === "GET",
    ).length;
    await createdPayoutAction.dblclick();
    await page.waitForFunction(() =>
      document
        .querySelector("#toast")
        ?.textContent.includes("Не удалось загрузить выплату #41"),
    );
    assert.equal(
      requestLog.filter(
        ({ path, method }) => path === "/api/admin/payouts/41" && method === "GET",
      ).length,
      detailRequestsBeforeFailure + 1,
    );
    assert.equal((await readPayoutState()).selectedPayoutId, 40);
    assert.equal((await readPayoutState()).detailPayoutId, 40);
    assert.deepEqual((await readPayoutState()).selectedUsers, [2, 3]);
    assert.equal((await readPayoutState()).activeMobileView, "composer");

    failedPayoutDetails.delete(41);
    const detailRequestBeforeSuccess = requestLog.filter(
      ({ path, method }) => path === "/api/admin/payouts/41" && method === "GET",
    ).length;
    const recipientsRequestBeforeSuccess = requestLog.filter(
      ({ path, method }) =>
        path === "/api/admin/payouts/41/recipients" && method === "GET",
    ).length;
    await createdPayoutAction.click();
    await page.waitForFunction(
      () =>
        document.querySelector(".nav-button.active")?.dataset.mobileView ===
        "users",
    );
    const createdSelectionState = await readPayoutState();
    assert.equal(createdSelectionState.selectedPayoutId, 41);
    assert.equal(createdSelectionState.detailPayoutId, 41);
    assert.equal(createdSelectionState.activeMobileView, "users");
    assert.deepEqual(createdSelectionState.selectedUsers, []);
    assert.deepEqual(createdSelectionState.selectedUsersByPayout41, []);
    assert.deepEqual(createdSelectionState.visibleUserChecks, []);
    assert.deepEqual(createdSelectionState.visibleSelectedRows, []);
    assert.equal(
      requestLog.filter(
        ({ path, method }) => path === "/api/admin/payouts/41" && method === "GET",
      ).length,
      detailRequestBeforeSuccess + 1,
    );
    assert.equal(
      requestLog.filter(
        ({ path, method }) =>
          path === "/api/admin/payouts/41/recipients" && method === "GET",
      ).length,
      recipientsRequestBeforeSuccess + 1,
    );
    assert.match(await page.locator("#selection-payout-context").innerText(), /#41/);
    assert.match(await page.locator("#recipients-context").innerText(), /#41/);

    await mkdir(evidence, { recursive: true });
    for (const theme of ["light", "dark"]) {
      await page.evaluate((scheme) => {
        const webApp = window.Telegram.WebApp;
        webApp.colorScheme = scheme;
        for (const callback of webApp.__callbacks.themeChanged || [])
          callback();
      }, theme);
      for (const [width, height] of [
        [390, 844],
        [1024, 768],
        [1440, 900],
      ]) {
        await page.setViewportSize({ width, height });
        await page.evaluate(() => window.scrollTo(0, 0));
        for (const tab of ["composer", "users", "recipients", "payouts"]) {
          await page.locator(`[data-mobile-view="${tab}"]`).click();
          await page.screenshot({
            path: resolve(evidence, `${theme}-${width}-${tab}.png`),
          });
        }
      }
    }
    assert.deepEqual(pageErrors, []);
    assert.equal(sendCalls, 3);
    assert.equal(markPaidCalls, 3);
    await t.diagnostic(`Mobile, tablet, and desktop light/dark screenshots saved to ${evidence}`);
    await context.close();
  } finally {
    await browser?.close();
    await new Promise((done, reject) =>
      server.close((error) => (error ? reject(error) : done())),
    );
  }
});
