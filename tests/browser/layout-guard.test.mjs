import assert from "node:assert/strict";
import { mkdir, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const evidence = process.env.BROWSER_EVIDENCE_DIR || resolve(tmpdir(), "topsbottg-browser-evidence");

// Independent layout regression: use production CSS and the actual payout-action markup contract.
// This catches squashed buttons or horizontal overflow without a backend or Telegram token.
for (const width of [320, 390, 768]) {
  for (const theme of ["light", "dark"]) {
    test(`payout action layout ${width}px ${theme}`, async () => {
      const browser = await chromium.launch({ headless: true });
      try {
        const page = await browser.newPage({ viewport: { width, height: 800 } });
        const html = `<!doctype html><html lang="ru" data-theme="${theme}"><head>
          <meta name="viewport" content="width=device-width, initial-scale=1">
          </head><body><main class="app-shell"><section class="workspace">
          <div id="history-current-context" class="payout-context">
            <span>Текущая: #125 · 01.11 – 15.11 · Ожидает подтверждения · Выплачено: 8 из 12</span>
            <div class="history-payout-actions">
              <button class="button button-quiet" data-action="clear-payout">Снять выбор</button>
              <button class="button button-quiet" data-action="delete-payout">Удалить выплату</button>
            </div>
          </div></section></main></body></html>`;
        await page.setContent(html);
        await page.addStyleTag({ content: await readFile(resolve(root, "frontend/styles.css"), "utf8") });
        const result = await page.evaluate(() => {
          const first = document.querySelector('[data-action="clear-payout"]').getBoundingClientRect();
          const second = document.querySelector('[data-action="delete-payout"]').getBoundingClientRect();
          const box = document.querySelector("#history-current-context").getBoundingClientRect();
          return {
            docWidth: document.documentElement.scrollWidth,
            viewport: innerWidth,
            first: { left: first.left, right: first.right, top: first.top, width: first.width },
            second: { left: second.left, right: second.right, top: second.top, width: second.width },
            box: { left: box.left, right: box.right },
          };
        });
        assert.ok(result.docWidth <= width, `Horizontal overflow: ${JSON.stringify(result)}`);
        assert.ok(result.first.width >= 90 && result.second.width >= 90, `Buttons too narrow: ${JSON.stringify(result)}`);
        assert.ok(Math.abs(result.first.top - result.second.top) <= 1, `Buttons should share a row: ${JSON.stringify(result)}`);
        assert.ok(result.first.right + 3 <= result.second.left, `Overlapping buttons: ${JSON.stringify(result)}`);
        assert.ok(result.first.left >= result.box.left && result.second.right <= result.box.right, `Actions outside context: ${JSON.stringify(result)}`);
        await mkdir(evidence, { recursive: true });
        await page.screenshot({ path: resolve(evidence, `payout-actions-${width}-${theme}.png`) });
        await page.close();
      } finally {
        await browser.close();
      }
    });
  }
}
