"""Text campaign API, deduplication and delivery semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import func, select

from topsbottg.models import (
    AdminGrant,
    CampaignRecipient,
    ChatMessage,
    ChatMessageOutbox,
    Conversation,
    MessageCampaign,
    Payout,
    PayoutRecipient,
    StudyGroup,
    User,
)
from topsbottg.worker import deliver_chat_outbox


async def _users(factory):
    async with factory() as session:
        group_a = StudyGroup(display_code="М3О-101", normalized_code="М3О-101")
        group_b = StudyGroup(display_code="М3О-102", normalized_code="М3О-102")
        session.add_all([group_a, group_b])
        await session.flush()
        users = [
            User(
                telegram_id=87000 + i, full_name=f"Студент {i}",
                registration_completed_at=datetime.now(UTC),
                study_group_id=group_a.id if i <= 3 else group_b.id if i <= 6 else None,
            )
            for i in range(1, 9)
        ]
        users.append(User(
            telegram_id=87999, full_name="Не зарегистрирован", study_group_id=group_a.id,
        ))
        session.add_all(users)
        await session.commit()
        return [u.id for u in users], group_a.id, group_b.id


def _headers(init_data: str, key: str = "client-operation") -> dict[str, str]:
    return {"X-Telegram-Init-Data": init_data, "Idempotency-Key": key}


class CampaignBot:
    def __init__(self, failure_by_chat=None):
        self.failure_by_chat = failure_by_chat or {}
        self.calls = []
        self.next_id = 300

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append((chat_id, text))
        failure = self.failure_by_chat.pop(chat_id, None)
        if isinstance(failure, Exception):
            raise failure
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)


@pytest.mark.asyncio
async def test_campaign_preview_and_snapshot_deduplicate_users(client, session_factory, admin_init_data):
    ids, a, b = await _users(session_factory)
    headers = _headers(admin_init_data)
    criteria = {"user_ids": [ids[0], ids[6], ids[0]], "study_group_ids": [a, b, a]}
    preview = await client.post("/api/admin/message-campaigns/preview", json=criteria, headers=headers)
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["recipient_count"] == 7
    assert [row["user_id"] for row in data["recipients"]] == ids[:7]
    assert data["recipients"][6]["study_group_name"] is None
    assert all(row["user_id"] != ids[-1] for row in data["recipients"])

    body = {**criteria, "text": "Объявление для группы"}
    result = await client.post("/api/admin/message-campaigns", json=body, headers=headers)
    assert result.status_code == 202, result.text
    campaign_id = result.json()["id"]
    assert result.json()["recipient_count"] == 7
    assert result.json()["queued_count"] == 7
    assert result.json()["status"] == "queued"

    async with session_factory() as session:
        assert await session.scalar(select(func.count(MessageCampaign.id))) == 1
        assert await session.scalar(select(func.count(CampaignRecipient.id))) == 7
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 7
        assert await session.scalar(select(func.count(ChatMessage.id))) == 7
        assert await session.scalar(select(func.count(Conversation.id))) == 7
        assert await session.scalar(select(func.count(Payout.id))) == 0
        assert await session.scalar(select(func.count(PayoutRecipient.id))) == 0
        u = await session.get(User, ids[0])
        u.study_group_id = b
        await session.commit()

    detail = await client.get(f"/api/admin/message-campaigns/{campaign_id}", headers=headers)
    assert detail.status_code == 200
    assert [row["user_id"] for row in detail.json()["recipients"]] == ids[:7]
    assert detail.json()["recipients"][0]["study_group_name"] == "М3О-101"
    assert len({row["conversation_id"] for row in detail.json()["recipients"]}) == 7

    duplicate = await client.post("/api/admin/message-campaigns", json={
        "user_ids": [ids[6], ids[0]], "study_group_ids": [b, a], "text": body["text"],
    }, headers=headers)
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == campaign_id
    assert (await client.post("/api/admin/message-campaigns", json={**body, "text": "Иной текст"},
                              headers=headers)).status_code == 409

    listed = await client.get("/api/admin/message-campaigns", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == campaign_id
    assert (await client.get("/api/admin/message-campaigns?cursor=bad", headers=headers)).status_code == 422
    assert (await client.get("/api/admin/message-campaigns/999999", headers=headers)).status_code == 404
    assert (await client.post("/api/admin/message-campaigns/999999/retry-failed",
                              headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_campaign_authorization_and_validation(
    client, session_factory, admin_init_data, non_admin_init_data, dynamic_admin_init_data
):
    ids, a, b = await _users(session_factory)
    data = {"user_ids": [ids[0]], "study_group_ids": [], "text": "Привет"}
    criteria = {"user_ids": [ids[0]], "study_group_ids": []}
    assert (await client.post("/api/admin/message-campaigns/preview", json=criteria)).status_code == 401
    assert (await client.post("/api/admin/message-campaigns/preview", json=criteria,
                              headers=_headers(non_admin_init_data))).status_code == 403
    assert (await client.post("/api/admin/message-campaigns", json=data,
                              headers=_headers(non_admin_init_data))).status_code == 403
    assert (await client.post("/api/admin/message-campaigns", json=data,
                              headers={"X-Telegram-Init-Data": admin_init_data})).status_code == 422
    for invalid in ["bad key", "x" * 129, ""]:
        assert (await client.post("/api/admin/message-campaigns", json=data,
                                  headers=_headers(admin_init_data, invalid))).status_code == 422
    for body in [
        {"user_ids": [], "study_group_ids": [], "text": "a"},
        {"user_ids": [ids[-1]], "study_group_ids": [], "text": "a"},
    ]:
        assert (await client.post("/api/admin/message-campaigns", json=body,
                                  headers=_headers(admin_init_data))).status_code == 422
    for body in [
        {"user_ids": ["1"], "study_group_ids": [], "text": "a"},
        {"user_ids": [-1], "study_group_ids": [], "text": "a"},
        {"user_ids": [ids[0]], "study_group_ids": [], "text": " "},
        {"user_ids": [ids[0]], "study_group_ids": [], "text": "a" * 4097},
    ]:
        assert (await client.post("/api/admin/message-campaigns", json=body,
                                  headers=_headers(admin_init_data))).status_code == 422
    assert (await client.post("/api/admin/message-campaigns/preview", json=criteria,
                              headers=_headers(dynamic_admin_init_data))).status_code == 403

    async with session_factory() as session:
        session.add(AdminGrant(
            telegram_id=555, granted_by_telegram_id=123, granted_at=datetime.now(UTC),
        ))
        await session.commit()
    dynamic = _headers(dynamic_admin_init_data)
    assert (await client.post("/api/admin/message-campaigns/preview", json=criteria,
                              headers=dynamic)).status_code == 200
    async with session_factory() as session:
        grant = await session.scalar(select(AdminGrant).where(AdminGrant.telegram_id == 555))
        grant.revoked_at = datetime.now(UTC)
        await session.commit()
    assert (await client.post("/api/admin/message-campaigns", json=data,
                              headers=dynamic)).status_code == 403
    assert (await client.get("/api/admin/message-campaigns",
                             headers=dynamic)).status_code == 403
    async with session_factory() as session:
        assert await session.scalar(select(func.count(MessageCampaign.id))) == 0


@pytest.mark.asyncio
async def test_campaign_delivery_and_retry_failed_only(client, session_factory, admin_init_data):
    ids, a, b = await _users(session_factory)
    headers = _headers(admin_init_data, "delivery")
    created = await client.post("/api/admin/message-campaigns", json={
        "user_ids": ids[:4], "study_group_ids": [], "text": "Проверка отправки",
    }, headers=headers)
    assert created.status_code == 202, created.text
    cid = created.json()["id"]
    method = SendMessage(chat_id=87001, text="Проверка отправки")
    bot = CampaignBot({
        87001: TelegramForbiddenError(method=method, message="Forbidden: bot was blocked"),
        87002: TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=44),
    })
    for _ in range(4):
        await deliver_chat_outbox(bot, session_factory)
    assert len(bot.calls) == 4
    detail = (await client.get(f"/api/admin/message-campaigns/{cid}", headers=headers)).json()
    assert detail["sent_count"] == 2
    assert detail["failed_count"] == 1
    assert detail["queued_count"] == 1
    assert detail["status"] == "sending"
    errors = {r["user_id"]: r["error_code"] for r in detail["recipients"]}
    assert errors[ids[0]] == "blocked"
    assert errors[ids[1]] == "rate_limited"

    retry = await client.post(f"/api/admin/message-campaigns/{cid}/retry-failed", headers=headers)
    assert retry.status_code == 200
    assert retry.json()["failed_count"] == 0
    assert retry.json()["queued_count"] == 2
    again = await client.post(f"/api/admin/message-campaigns/{cid}/retry-failed", headers=headers)
    assert again.status_code == 200
    async with session_factory() as session:
        assert await session.scalar(select(func.count(CampaignRecipient.id))) == 4
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 4
        assert await session.scalar(select(func.count(ChatMessage.id))) == 4
        row = await session.scalar(
            select(CampaignRecipient).where(CampaignRecipient.user_id == ids[0])
        )
        assert row.retry_count == 1
        row2 = await session.scalar(
            select(ChatMessageOutbox).join(CampaignRecipient).where(CampaignRecipient.user_id == ids[1])
        )
        assert row2.error_code == "rate_limited" and row2.status == "queued"
        row2.retry_at = datetime.now(UTC)
        await session.commit()
    await deliver_chat_outbox(bot, session_factory)
    await deliver_chat_outbox(bot, session_factory)
    detail = (await client.get(f"/api/admin/message-campaigns/{cid}", headers=headers)).json()
    assert detail["status"] == "sent" and detail["sent_count"] == 4
    assert all(item["status"] == "sent" for item in detail["recipients"])
    assert len(bot.calls) == 6

    # Delivery history remains in the sole permanent conversation of each recipient.
    for item in detail["recipients"]:
        history = await client.get(
            f"/api/admin/conversations/{item['conversation_id']}/messages", headers=headers,
        )
        assert history.status_code == 200
        assert len(history.json()["items"]) == 1
        assert history.json()["items"][0]["id"] == item["message_id"]
        assert history.json()["items"][0]["delivery_status"] == "sent"
