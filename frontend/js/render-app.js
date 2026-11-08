import { renderAuthState } from "./telegram.js";
import {
  renderActionState,
  renderCurrentPayout,
  renderNotifications,
  renderPreview,
  renderPayouts,
} from "./render-common.js";
import { renderRecipients } from "./render-recipients.js";
import { renderUsers } from "./render-users.js";
import { state, $ } from "./store.js";
const titles = {
  composer: "Создать выплату",
  users: "Выбор пользователей",
  recipients: "Получатели",
  payouts: "История выплат",
};
export function renderNavigation() {
  document.querySelectorAll("[data-screen]").forEach((el) => {
    el.hidden = el.dataset.screen !== state.activeMobileView;
  });
  document.querySelectorAll("[data-mobile-view]").forEach((el) => {
    const active = el.dataset.mobileView === state.activeMobileView;
    el.classList.toggle("active", active);
    el.setAttribute("aria-pressed", String(active));
  });
  const title = $("screen-title");
  if (title) title.textContent = titles[state.activeMobileView] || "Выплаты";
  renderActionState();
}
export function renderApp() {
  renderNotifications();
  renderAuthState();
  renderPreview();
  renderUsers();
  renderCurrentPayout();
  renderRecipients();
  renderPayouts();
  renderNavigation();
}
