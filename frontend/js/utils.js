export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function statusLabel(map, value, fallback = "Неизвестный статус") {
  return map[value] || fallback;
}

export function badgeClassForStatus(status) {
  if (["paid", "payment_received"].includes(status)) return "badge-success";
  if (["sent", "sending", "payment_required", "pending"].includes(status))
    return "badge-warning";
  if (["failed", "cancelled", "partially_failed"].includes(status))
    return "badge-danger";
  return "badge-muted";
}

export function initials(name) {
  const parts = String(name).trim().split(/\s+/).filter(Boolean).slice(0, 2);
  if (!parts.length) return "TB";
  return parts.map((part) => part[0].toUpperCase()).join("");
}

export function formatTime(value) {
  const date = value ? new Date(value) : new Date();
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function emptyStateMarkup(title, text) {
  return `
    <div class="empty-state">
      <div class="empty-state-dot"></div>
      <div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(text)}</p>
      </div>
    </div>`;
}

export function loadingStateMarkup(title, text = "Загрузка…") {
  return `
    <div class="loading-state">
      <span class="spinner" aria-hidden="true"></span>
      <div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(text)}</p>
      </div>
    </div>`;
}

export function parsePeriodInput(value) {
  const match = /^(\d{2})\.(\d{2})$/.exec(String(value));
  if (!match) return null;
  const day = Number(match[1]);
  const month = Number(match[2]);
  const daysPerMonth = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return month >= 1 && month <= 12 && day >= 1 && day <= daysPerMonth[month - 1]
    ? { day, month }
    : null;
}

export function plural(n, one, few, many) {
  const value = Math.abs(n) % 100;
  const lastDigit = value % 10;
  if (value >= 11 && value <= 14) return many;
  return lastDigit === 1 ? one : lastDigit >= 2 && lastDigit <= 4 ? few : many;
}

export function formatPayoutPeriodLabel(
  startDay,
  startMonth,
  endDay,
  endMonth,
) {
  const start = `${String(startDay).padStart(2, "0")}.${String(startMonth).padStart(2, "0")}`;
  const end = `${String(endDay).padStart(2, "0")}.${String(endMonth).padStart(2, "0")}`;
  return start === end ? start : `${start} – ${end}`;
}
