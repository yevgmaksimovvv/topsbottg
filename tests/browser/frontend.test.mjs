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
  let sendCalls = 0;
  const sentUserIds = [];
  let markPaidCalls = 0;
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
        data = {
          id: 39,
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
        data = { payout: payouts.find((payout) => payout.id === id) || null };
      } else if (path.endsWith("/recipients") && method === "GET") {
        data =
          Number(path.split("/").at(-2)) === 38
            ? historicalRecipients
            : recipients;
      } else if (path.endsWith("/send-selected")) {
        sendCalls += 1;
        sentUserIds.push(req.postDataJSON().user_ids);
        await new Promise((done) => setTimeout(done, 100));
        const payout = payouts.find(
          (item) => item.id === Number(path.split("/").at(-2)),
        );
        payout.status = "sent";
        recipients = req.postDataJSON().user_ids.map((userId, index) => ({
          id: 600 + index,
          user_id: userId,
          full_name:
            users.find((user) => user.id === userId)?.full_name ||
            `Получатель ${userId}`,
          status: "sent",
        }));
        data = { payout, recipients };
      } else if (path.endsWith("/mark-paid")) {
        markPaidCalls += 1;
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
    await page.locator('[data-action="go-selection"]').click();

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
    await page.locator('[data-payout-id="40"]').click();
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
    await page.locator('[data-payout-id="39"]').click();
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
    await page.locator('[data-payout-id="40"]').click();
    await page.locator('[data-mobile-view="users"]').click();
    await assertVisibleSelection([2, 3]);
    await page.locator('[data-mobile-view="payouts"]').click();
    await page.locator('[data-payout-id="39"]').click();
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
    await page.locator('[data-payout-id="37"]').click();
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
    await page.locator('[data-payout-id="39"]').click();
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
    await page.locator('[data-payout-id="38"]').click();
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
    assert.equal(sendCalls, 1);
    assert.equal(markPaidCalls, 1);
    await t.diagnostic(`Mobile, tablet, and desktop light/dark screenshots saved to ${evidence}`);
    await context.close();
  } finally {
    await browser?.close();
    await new Promise((done, reject) =>
      server.close((error) => (error ? reject(error) : done())),
    );
  }
});
