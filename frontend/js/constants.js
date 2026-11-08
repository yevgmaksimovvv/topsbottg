export const PAYOUT_STATUS_LABELS = {
  draft: "Черновик",
  sending: "Рассылка идёт",
  sent: "Разослана",
  partially_failed: "Есть ошибки отправки",
  completed: "Завершена",
  cancelled: "Отменена",
};

export const RECIPIENT_STATUS_LABELS = {
  pending: "Ожидает отправки",
  sending: "Отправляется",
  sent: "Ожидает подтверждения данных",
  payment_required: "Нужны платёжные данные",
  payment_received: "Данные подтверждены · к оплате",
  paid: "Выплачено",
  failed: "Ошибка отправки",
  cancelled: "Отменён",
};

export const AUTO_HIDE_TOAST_MS = 3200;
export const API_TIMEOUT_MS = 15000;
export const USERS_LIMIT = 50;
export const SEARCH_DEBOUNCE_MS = 300;
export const MOBILE_QUERY = window.matchMedia("(max-width: 759px)");

export const COMPOSER_IDS = {
  periodStart: "period-start",
  periodEnd: "period-end",
  messageBefore: "message-before",
  messageAfter: "message-after",
  messageCount: "message-count",
  validation: "payout-validation",
  preview: "preview",
};
