export const PAYOUT_STATUS_LABELS = {
  draft: "Черновик",
  sending: "Рассылка идёт",
  sent: "Разослана",
  partially_failed: "Есть ошибки отправки",
  completed: "Завершена",
  cancelled: "Отменена",
};

export const PAYOUT_WORKFLOW_LABELS = {
  draft: "\u0427\u0435\u0440\u043d\u043e\u0432\u0438\u043a", sending: "\u0420\u0430\u0441\u0441\u044b\u043b\u0430\u0435\u0442\u0441\u044f", delivery_errors: "\u041e\u0448\u0438\u0431\u043a\u0438 \u0440\u0430\u0441\u0441\u044b\u043b\u043a\u0438",
  awaiting_confirmation: "\u041e\u0436\u0438\u0434\u0430\u0435\u0442 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f", ready_for_payment: "\u0413\u043e\u0442\u043e\u0432\u0430 \u043a \u043e\u043f\u043b\u0430\u0442\u0435",
  closed: "Закрыта", paid: "\u0412\u044b\u043f\u043b\u0430\u0447\u0435\u043d\u0430", cancelled: "\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u0430", no_recipients: "\u041d\u0435\u0442 \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u0435\u0439", no_active_recipients: "\u041d\u0435\u0442 \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u0435\u0439",
};

export const RECIPIENT_STATUS_LABELS = {
  pending: "Ожидает отправки",
  sending: "Отправляется",
  sent: "Ожидает подтверждения данных",
  payment_required: "Нужны платёжные данные",
  payment_received: "\u0413\u043e\u0442\u043e\u0432 \u043a \u043e\u043f\u043b\u0430\u0442\u0435",
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
