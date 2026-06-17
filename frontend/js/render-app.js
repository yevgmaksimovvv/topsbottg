import { renderAuthState } from "./telegram.js";
import { renderActionState, renderCurrentPayout, renderMobileView, renderNotifications, renderPreview } from "./render-common.js";
import { renderPayouts } from "./render-payouts.js";
import { renderRecipients } from "./render-recipients.js";
import { renderUsers } from "./render-users.js";

export function renderApp() {
  renderNotifications();
  renderAuthState();
  renderPreview();
  renderUsers();
  renderCurrentPayout();
  renderPayouts();
  renderRecipients();
  renderActionState();
  renderMobileView();
}
