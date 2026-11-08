import { canUseApi, state } from "./store.js";

export function telegramWebApp() {
  return window.Telegram?.WebApp || null;
}

function themeColor(name, fallback) {
  return telegramWebApp()?.themeParams?.[name] || fallback;
}

export function setThemeVariables() {
  const webApp = telegramWebApp();
  const dark = webApp?.colorScheme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const root = document.documentElement.style;
  root.setProperty(
    "--bg",
    themeColor("bg_color", dark ? "#192229" : "#F4F6F8"),
  );
  root.setProperty(
    "--surface",
    themeColor("secondary_bg_color", dark ? "#27323B" : "#FFFFFF"),
  );
  root.setProperty(
    "--field",
    themeColor("section_bg_color", dark ? "#202A32" : "#F0F3F6"),
  );
  root.setProperty(
    "--text",
    themeColor("text_color", dark ? "#EDF2F6" : "#17212B"),
  );
  root.setProperty(
    "--muted",
    themeColor("hint_color", dark ? "#B0BEC9" : "#52616F"),
  );
  root.setProperty(
    "--accent",
    themeColor("button_color", dark ? "#59A8E8" : "#267FC1"),
  );
  root.setProperty(
    "--accent-contrast",
    themeColor("button_text_color", dark ? "#10212d" : "#FFFFFF"),
  );
  root.setProperty(
    "--button-bg",
    themeColor("button_color", dark ? "#59A8E8" : "#1D6EA6"),
  );
  root.setProperty("--success", dark ? "#53c98b" : "#14703e");
  root.setProperty("--warning", dark ? "#edbd58" : "#805900");
  root.setProperty(
    "--danger",
    themeColor("destructive_text_color", dark ? "#ff747e" : "#b42332"),
  );
  root.setProperty(
    "--border",
    dark ? "rgba(255,255,255,.09)" : "rgba(0,0,0,.10)",
  );
}

export function syncViewportVariables() {
  const webApp = telegramWebApp();
  const root = document.documentElement.style;
  const height = Math.round(
    webApp?.viewportHeight ||
      window.visualViewport?.height ||
      window.innerHeight ||
      0,
  );
  root.setProperty("--app-viewport-height", `${height}px`);
  const visualViewport = window.visualViewport;
  const keyboardInset = visualViewport
    ? Math.max(
        0,
        Math.round(
          window.innerHeight - visualViewport.height - visualViewport.offsetTop,
        ),
      )
    : 0;
  root.setProperty("--keyboard-inset-bottom", `${keyboardInset}px`);
  const safe = Math.max(0, Number(webApp?.safeAreaInset?.bottom) || 0);
  const contentSafe = Math.max(
    0,
    Number(webApp?.contentSafeAreaInset?.bottom) || 0,
  );
  root.setProperty("--tg-safe-area-inset-bottom", `${safe}px`);
  root.setProperty("--tg-content-safe-area-inset-bottom", `${contentSafe}px`);
}

export function bindTelegramViewportEvents(onChange = syncViewportVariables) {
  const webApp = telegramWebApp();
  const sync = () => onChange();
  sync();
  for (const eventName of [
    "viewportChanged",
    "safeAreaChanged",
    "contentSafeAreaChanged",
  ]) {
    try {
      webApp?.onEvent?.(eventName, sync);
    } catch {
      /* Older Telegram clients may not support this event. */
    }
  }
  try {
    webApp?.onEvent?.("themeChanged", () => {
      setThemeVariables();
      sync();
    });
  } catch {
    /* Optional Telegram event. */
  }
  window.addEventListener("resize", sync, { passive: true });
  window.visualViewport?.addEventListener("resize", sync, { passive: true });
}

export function renderAuthState() {
  const banner = document.getElementById("auth-banner");
  if (!banner) return;
  const message = canUseApi()
    ? ""
    : state.authStatus === "telegram_missing_init_data"
      ? "Telegram не передал данные авторизации. Откройте Mini App через кнопку бота."
      : "Откройте приложение через Telegram, чтобы загрузить данные.";
  banner.textContent = message;
  banner.classList.toggle("hidden", !message);
}
