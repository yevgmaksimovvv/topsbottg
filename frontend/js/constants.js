export const PAYOUT_STATUS_LABELS = {
  draft: "Черновик",
  sending: "Рассылка идет",
  sent: "Разослана",
  partially_failed: "Часть отправок не удалась",
  completed: "Завершена",
  cancelled: "Отменена",
};

export const RECIPIENT_STATUS_LABELS = {
  pending: "Ожидает рассылки",
  sending: "Отправляется",
  sent: "Ожидает подтверждения данных",
  payment_required: "Ожидает заполнения данных",
  payment_received: "Данные подтверждены",
  paid: "Выплачено",
  failed: "Ошибка отправки",
  cancelled: "Отменено",
};

export const AUTO_HIDE_TOAST_MS = 3200;
export const API_TIMEOUT_MS = 15000;
export const USERS_LIMIT = 50;
export const SEARCH_DEBOUNCE_MS = 300;
export const POLL_INTERVAL_MS = 4000;
export const MOBILE_QUERY = window.matchMedia("(max-width: 759px)");

export const COMPOSER_IDS = {
  desktop: {
    periodStartDay: "desktop-period-start-day",
    periodStartMonth: "desktop-period-start-month",
    periodEndDay: "desktop-period-end-day",
    periodEndMonth: "desktop-period-end-month",
    messageTemplate: "desktop-message-template",
    validation: "desktop-payout-validation",
    preview: "desktop-preview",
  },
  mobile: {
    periodStartDay: "mobile-period-start-day",
    periodStartMonth: "mobile-period-start-month",
    periodEndDay: "mobile-period-end-day",
    periodEndMonth: "mobile-period-end-month",
    messageTemplate: "mobile-message-template",
    validation: "mobile-payout-validation",
    preview: "mobile-preview",
  },
};
