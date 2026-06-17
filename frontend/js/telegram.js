import { canUseApi, state } from "./store.js";

export function telegramWebApp() {
  return window.Telegram?.WebApp || null;
}

export function setThemeVariables() {
  const theme = telegramWebApp()?.themeParams || {};
  const root = document.documentElement.style;
  root.setProperty("--bg", theme.bg_color || "#0e1621");
  root.setProperty("--surface", theme.secondary_bg_color || "#17212b");
  root.setProperty("--surface-2", theme.section_bg_color || "#1f2c38");
  root.setProperty("--surface-3", "#233445");
  root.setProperty("--text", theme.text_color || "#f3f7fb");
  root.setProperty("--muted", theme.hint_color || "#8ea2b5");
  root.setProperty("--primary", theme.button_color || "#2ea6ff");
  root.setProperty("--primary-contrast", theme.button_text_color || "#06111a");
  root.setProperty("--success", "#35c779");
  root.setProperty("--warning", "#f2b84b");
  root.setProperty("--danger", theme.destructive_text_color || "#ff5c7a");
  root.setProperty("--border", "rgba(255,255,255,.08)");
  root.setProperty("--radius", "18px");
  root.setProperty("--shadow", "0 12px 28px rgba(0,0,0,.24)");
}

export function syncViewportVariables() {
  const root = document.documentElement.style;
  const webApp = telegramWebApp();
  const viewportHeight = Math.max(
    0,
    Math.round(webApp?.viewportHeight || window.visualViewport?.height || window.innerHeight || document.documentElement.clientHeight)
  );
  root.setProperty("--app-viewport-height", `${viewportHeight}px`);
  const mobileNav = document.querySelector(".mobile-nav");
  if (mobileNav) {
    root.setProperty("--mobile-nav-height", `${Math.ceil(mobileNav.getBoundingClientRect().height)}px`);
  }
}

export function bindTelegramViewportEvents(onChange = syncViewportVariables) {
  const webApp = telegramWebApp();
  const sync = () => onChange();
  sync();
  webApp?.onEvent?.("viewportChanged", sync);
  window.addEventListener("resize", sync, { passive: true });
  window.addEventListener("orientationchange", sync, { passive: true });
  window.visualViewport?.addEventListener("resize", sync, { passive: true });
  window.visualViewport?.addEventListener("scroll", sync, { passive: true });
}

export function renderAuthState() {
  const pill = document.getElementById("auth-state");
  const banner = document.getElementById("auth-banner");
  if (!pill || !banner) return;
  if (canUseApi()) {
    pill.textContent = "Telegram подключён";
    pill.className = "auth-pill auth-ok";
    pill.classList.remove("hidden");
    banner.classList.add("hidden");
    banner.textContent = "";
  } else if (state.authStatus === "telegram_missing_init_data") {
    pill.textContent = "Telegram открыт";
    pill.className = "auth-pill auth-warn";
    pill.classList.remove("hidden");
    banner.textContent = "Telegram открыт, но не передал данные авторизации. Откройте мини-приложение через кнопку бота.";
    banner.classList.remove("hidden");
  } else {
    pill.textContent = "";
    pill.className = "auth-pill hidden";
    banner.textContent = "Откройте через Telegram, чтобы загрузить данные.";
    banner.classList.remove("hidden");
  }
}
