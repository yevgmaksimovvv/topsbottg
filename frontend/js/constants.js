export const PAYOUT_STATUS_LABELS = {
  draft: "черновик",
  sending: "идет рассылка",
  sent: "отправлено",
  partially_failed: "часть отправок не удалась",
  completed: "закрыта админом",
  cancelled: "отменена",
};

export const RECIPIENT_STATUS_LABELS = {
  pending: "ожидает отправки",
  sending: "отправляется",
  sent: "сообщение отправлено",
  failed: "ошибка отправки",
  payment_required: "нужны данные",
  payment_received: "данные получены",
  paid: "выплачено",
  cancelled: "исключен из выплаты",
};

export const AUTO_HIDE_TOAST_MS = 3200;
export const USERS_LIMIT = 50;
export const SEARCH_DEBOUNCE_MS = 300;
export const POLL_INTERVAL_MS = 4000;
export const MOBILE_QUERY = window.matchMedia("(max-width: 759px)");

export const COMPOSER_IDS = {
  desktop: {
    title: "desktop-payout-title",
    periodFrom: "desktop-period-from",
    periodTo: "desktop-period-to",
    messageTemplate: "desktop-message-template",
    validation: "desktop-payout-validation",
    preview: "desktop-preview",
  },
  mobile: {
    title: "mobile-payout-title",
    periodFrom: "mobile-period-from",
    periodTo: "mobile-period-to",
    messageTemplate: "mobile-message-template",
    validation: "mobile-payout-validation",
    preview: "mobile-preview",
  },
};
