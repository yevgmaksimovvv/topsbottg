const BASE = "/admin/conversations";

export function createInboxApiClient(request) {
  if (typeof request !== "function") throw new TypeError("Expected an authenticated request function");

  return {
    summary: ({ signal } = {}) => request(BASE + "/summary", { signal }),
    conversations({ query = "", study_group_id = "", cursor = null, limit = 50, signal } = {}) {
      const params = new URLSearchParams({ limit: String(limit) });
      if (query.trim()) params.set("query", query.trim());
      if (study_group_id) params.set("study_group_id", String(study_group_id));
      if (cursor != null) params.set("cursor", cursor);
      return request(BASE + "?" + params.toString(), { signal });
    },
    users({ search = "", study_group_id = "", offset = 0, limit = 50, signal } = {}) {
      const params = new URLSearchParams({ offset: String(offset), limit: String(limit), registered_only: "true" });
      if (search.trim()) params.set("search", search.trim());
      if (study_group_id) params.set("study_group_id", String(study_group_id));
      return request("/admin/users?" + params.toString(), { signal });
    },
    open(user_id, { signal } = {}) {
      return request(BASE, {
        method: "POST",
        signal,
        body: JSON.stringify({ user_id }),
      });
    },
    messages(conversation_id, { before = null, limit = 50, signal } = {}) {
      const params = new URLSearchParams({ limit: String(limit) });
      if (before != null) params.set("before", before);
      return request(BASE + "/" + encodeURIComponent(conversation_id) + "/messages?" + params.toString(), { signal });
    },
    send(conversation_id, { text, reply_to_message_id, idempotency_key }) {
      const payload = { text };
      if (reply_to_message_id != null) payload.reply_to_message_id = reply_to_message_id;
      return request(BASE + "/" + encodeURIComponent(conversation_id) + "/messages", {
        method: "POST",
        headers: { "Idempotency-Key": idempotency_key },
        body: JSON.stringify(payload),
      });
    },
    read(conversation_id, last_read_message_id, { signal } = {}) {
      return request(BASE + "/" + encodeURIComponent(conversation_id) + "/read", {
        method: "POST",
        body: JSON.stringify({ last_read_message_id }),
        signal,
      });
    },
  };
}
