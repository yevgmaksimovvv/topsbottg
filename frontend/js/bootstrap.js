import { SEARCH_DEBOUNCE_MS } from "./constants.js";
import { renderApp } from "./render-app.js";
import { setThemeVariables, telegramWebApp } from "./telegram.js";
import { refreshTelegramAuthState, state, setMobileView, syncComposerFields, syncFilterInputs } from "./store.js";
import {
  attachSelected,
  closePaymentModal,
  copyPaymentDetails,
  createPayout,
  exportCsv,
  loadPayouts,
  loadUsers,
  markPaid,
  revealPaymentDetails,
  selectPayout,
  sendPayout,
} from "./api.js";

function bindComposerInputs() {
  const ids = [
    ["desktop-payout-title", "title"],
    ["desktop-period-from", "periodFrom"],
    ["desktop-period-to", "periodTo"],
    ["desktop-message-template", "messageTemplate"],
    ["mobile-payout-title", "title"],
    ["mobile-period-from", "periodFrom"],
    ["mobile-period-to", "periodTo"],
    ["mobile-message-template", "messageTemplate"],
  ];

  ids.forEach(([id, key]) => {
    document.getElementById(id)?.addEventListener("input", (event) => {
      state.composer[key] = event.target.value;
      syncComposerFields();
      renderApp();
    });
  });
}

function bindFilterInputs() {
  const searchIds = ["desktop-search", "mobile-search"];
  const paymentIds = ["desktop-has-payment", "mobile-has-payment"];
  const activeIds = ["desktop-is-active", "mobile-is-active"];

  searchIds.forEach((id) => {
    document.getElementById(id)?.addEventListener("input", (event) => {
      state.usersSearch = event.target.value;
      syncFilterInputs();
      if (state.searchTimer) clearTimeout(state.searchTimer);
      state.searchTimer = window.setTimeout(() => loadUsers({ reset: true }), SEARCH_DEBOUNCE_MS);
      renderApp();
    });
  });

  paymentIds.forEach((id) => {
    document.getElementById(id)?.addEventListener("change", (event) => {
      state.usersHasPayment = event.target.value;
      syncFilterInputs();
      loadUsers({ reset: true });
      renderApp();
    });
  });

  activeIds.forEach((id) => {
    document.getElementById(id)?.addEventListener("change", (event) => {
      state.usersIsActive = event.target.value;
      syncFilterInputs();
      loadUsers({ reset: true });
      renderApp();
    });
  });
}

function bindDelegatedEvents() {
  document.addEventListener("click", async (event) => {
    const action = event.target.closest("[data-action]");
    const mobileView = event.target.closest("[data-mobile-view]");
    const userCheckbox = event.target.closest('input[type="checkbox"][data-user-id]');
    const payoutButton = event.target.closest("[data-payout-id]");
    const modal = event.target.closest("#payment-modal");

    if (mobileView) {
      setMobileView(mobileView.dataset.mobileView);
      renderApp();
      return;
    }

    if (userCheckbox) return;

    if (payoutButton) {
      await selectPayout(Number(payoutButton.dataset.payoutId));
      renderApp();
      return;
    }

    if (action) {
      const { action: name, userId, recipientId } = action.dataset;
      if (name === "reload-users") {
        await loadUsers({ reset: true });
      } else if (name === "load-more-users") {
        await loadUsers({ reset: false });
      } else if (name === "clear-selection") {
        state.selectedUsers.clear();
        renderApp();
      } else if (name === "create-payout") {
        await createPayout();
      } else if (name === "attach-selected") {
        await attachSelected();
      } else if (name === "send-payout") {
        await sendPayout();
      } else if (name === "export-csv") {
        await exportCsv();
      } else if (name === "reveal-payment") {
        await revealPaymentDetails(Number(userId));
      } else if (name === "mark-paid") {
        await markPaid(Number(recipientId));
      } else if (name === "copy-payment-details") {
        await copyPaymentDetails();
      } else if (name === "close-payment-modal") {
        closePaymentModal();
      } else if (name === "close-payment-modal-secondary") {
        closePaymentModal();
      }
      renderApp();
      return;
    }

    if (modal && event.target === modal) {
      closePaymentModal();
      renderApp();
    }
  });

  document.addEventListener("change", (event) => {
    const checkbox = event.target.closest('input[type="checkbox"][data-user-id]');
    if (!checkbox) return;
    const id = Number(checkbox.dataset.userId);
    if (checkbox.checked) state.selectedUsers.add(id);
    else state.selectedUsers.delete(id);
    renderApp();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePaymentModal();
      renderApp();
    }
  });
}

export async function bootstrap() {
  setThemeVariables();
  const webApp = telegramWebApp();
  if (webApp) {
    webApp.ready();
    webApp.expand();
  }
  refreshTelegramAuthState();

  state.composer.messageTemplate =
    "Всем привет!\n" +
    "Выплачиваем ЗАРПЛАТУ за работу в период с {period_from} по {period_to}.\n\n" +
    "Для получения выплаты проверьте или заполните платежные данные.\n\n" +
    "Если перевод на ваши данные:\n" +
    "укажите фамилию и имя, номер телефона для перевода / СБП, банк.\n\n" +
    "Если перевод на чужие данные:\n" +
    "укажите ваши фамилию и имя, имя владельца, номер телефона владельца, банк.\n\n" +
    "После сохранения бот должен ответить: «Ваше сообщение сохранено».";

  syncFilterInputs();
  syncComposerFields();
  bindComposerInputs();
  bindFilterInputs();
  bindDelegatedEvents();
  renderApp();

  if (!state.initData) return;
  await Promise.all([loadUsers({ reset: true }), loadPayouts()]);
}
