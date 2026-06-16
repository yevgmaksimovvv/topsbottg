export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function statusLabel(map, value) {
  return map[value] || value;
}

export function badgeClassForStatus(status) {
  if (["paid", "sent"].includes(status)) return "badge-success";
  if (["sending", "payment_received"].includes(status)) return "badge-warning";
  if (["failed", "cancelled", "partially_failed"].includes(status)) return "badge-danger";
  return "badge-muted";
}

export function initials(name) {
  const parts = String(name)
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2);
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
