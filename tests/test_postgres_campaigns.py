"""PostgreSQL snapshot, concurrent campaign creation and outbox consistency."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.api import create_app
from topsbottg.chat_outbox import claim_chat_outbox, settle_chat_outbox
from topsbottg.models import (
    AdminGrant,
    Base,
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


@pytest.fixture
async def postgres_campaign(settings):
    url = os.getenv("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL required")
    schema = f"ci_campaign_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        app = create_app(settings.model_copy(update={"database_url": url}), factory)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client, factory() as session:
            a = StudyGroup(display_code="Группа A", normalized_code="GROUP-A")
            b = StudyGroup(display_code="Группа B", normalized_code="GROUP-B")
            session.add_all([a, b])
            await session.flush()
            users = [
                User(
                    telegram_id=88000 + index, full_name=f"Recipient {index}",
                    study_group_id=a.id if index <= 10 else b.id if index <= 19 else None,
                    registration_completed_at=datetime.now(UTC),
                )
                for index in range(1, 21)
            ]
            session.add_all(users)
            await session.commit()
            yield client, factory, [u.id for u in users], a.id, b.id
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


def _headers(init_data, key="concurrent-post"):
    return {"X-Telegram-Init-Data": init_data, "Idempotency-Key": key}


def _body(ids, a, b, *, text="Всем зарегистрированным"):
    return {"user_ids": [ids[0], ids[19], ids[0]], "study_group_ids": [a, b, a], "text": text}


@pytest.mark.postgres
async def test_pg_parallel_campaign_post_has_one_frozen_snapshot(postgres_campaign, admin_init_data):
    client, factory, ids, a, b = postgres_campaign
    body = _body(ids, a, b)
    headers = _headers(admin_init_data)
    results = await asyncio.wait_for(asyncio.gather(*[
        client.post("/api/admin/message-campaigns", json=body, headers=headers)
        for _ in range(8)
    ]), timeout=35)
    assert [r.status_code for r in results] == [202] * 8, [r.text for r in results]
    campaign_ids = {r.json()["id"] for r in results}
    assert len(campaign_ids) == 1
    campaign_id = next(iter(campaign_ids))
    assert all(r.json()["recipient_count"] == 20 for r in results)

    async with factory() as session:
        assert await session.scalar(select(func.count(MessageCampaign.id))) == 1
        assert await session.scalar(select(func.count(CampaignRecipient.id))) == 20
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 20
        assert await session.scalar(select(func.count(ChatMessage.id))) == 20
        assert await session.scalar(select(func.count(Conversation.id))) == 20
        assert await session.scalar(select(func.count(Payout.id))) == 0
        assert await session.scalar(select(func.count(PayoutRecipient.id))) == 0
        recipients = (await session.scalars(
            select(CampaignRecipient).order_by(CampaignRecipient.user_id)
        )).all()
        assert [rec.user_id for rec in recipients] == ids
        assert len({rec.outbox_id for rec in recipients}) == 20
        user = await session.get(User, ids[0])
        user.study_group_id = b
        await session.commit()

    detail = await client.get(f"/api/admin/message-campaigns/{campaign_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["recipients"][0]["study_group_name"] == "Группа A"
    assert detail.json()["recipient_count"] == 20
    repeat = await client.post("/api/admin/message-campaigns", json=body, headers=headers)
    assert repeat.status_code == 202 and repeat.json()["id"] == campaign_id
    conflict = await client.post("/api/admin/message-campaigns", json=_body(ids, a, b, text="Другой текст"),
                                 headers=headers)
    assert conflict.status_code == 409

    # Different actor, same operation key, is independent.
    async with factory() as session:
        session.add(AdminGrant(telegram_id=555, granted_by_telegram_id=123,
                               granted_at=datetime.now(UTC)))
        await session.commit()


@pytest.mark.postgres
async def test_pg_claims_are_exclusive_and_statuses_come_from_outbox(
    postgres_campaign, admin_init_data
):
    client, factory, ids, a, b = postgres_campaign
    headers = _headers(admin_init_data, key="claim-test")
    response = await client.post("/api/admin/message-campaigns", json={
        "user_ids": ids[:2], "study_group_ids": [], "text": "Отправка",
    }, headers=headers)
    assert response.status_code == 202
    cid = response.json()["id"]
    async with factory() as first:
        [claim] = await claim_chat_outbox(first, limit=1)
        async with factory() as second:
            other = await asyncio.wait_for(claim_chat_outbox(second, limit=1), timeout=5)
            assert len(other) == 1 and other[0].id != claim.id
            # Neither session has yet committed the claims.
            await second.commit()
        await first.commit()
    async with factory() as session:
        statuses = (await session.scalars(
            select(CampaignRecipient.status).where(CampaignRecipient.campaign_id == cid)
        )).all()
        assert statuses == ["sending", "sending"]

    async with factory() as session:
        assert await settle_chat_outbox(session, claim, telegram_message_id=901)
        await session.commit()
    async with factory() as session:
        detail = await session.scalar(
            select(CampaignRecipient).where(CampaignRecipient.outbox_id == claim.id)
        )
        assert detail.status == "sent" and detail.error_code is None
        second_claim = other[0]
        assert await settle_chat_outbox(
            session, second_claim, error_code="blocked", permanent=True
        )
        await session.commit()
    mixed = (await client.get(f"/api/admin/message-campaigns/{cid}", headers=headers)).json()
    assert mixed["status"] == "partially_failed"
    assert mixed["sent_count"] == mixed["failed_count"] == 1
    assert {r["status"] for r in mixed["recipients"]} == {"sent", "failed"}

    # Two simultaneous retry requests must never create two jobs.
    retried = await asyncio.wait_for(asyncio.gather(
        client.post(f"/api/admin/message-campaigns/{cid}/retry-failed", headers=headers),
        client.post(f"/api/admin/message-campaigns/{cid}/retry-failed", headers=headers),
    ), timeout=15)
    assert [r.status_code for r in retried] == [200, 200]
    async with factory() as session:
        assert await session.scalar(select(func.count(CampaignRecipient.id))) == 2
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 2
        assert await session.scalar(select(func.count(ChatMessage.id))) == 2
        recipient = await session.scalar(
            select(CampaignRecipient).where(CampaignRecipient.outbox_id == second_claim.id)
        )
        assert recipient.retry_count == 1
        assert recipient.status == "queued"
        sent = await session.get(ChatMessageOutbox, claim.id)
        assert sent.status == "sent" and sent.telegram_message_id == 901
    after = (await client.get(f"/api/admin/message-campaigns/{cid}", headers=headers)).json()
    assert after["queued_count"] == 1 and after["sent_count"] == 1


@pytest.mark.postgres
async def test_pg_campaign_failed_creation_rolls_back_every_outbox(
    postgres_campaign, admin_init_data, monkeypatch
):
    import topsbottg.campaigns as campaigns

    client, factory, ids, a, b = postgres_campaign
    original = campaigns.create_outgoing_message
    calls = 0

    async def fail_halfway(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("simulated outbox insert failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(campaigns, "create_outgoing_message", fail_halfway)
    with pytest.raises(RuntimeError, match="simulated outbox"):
        await client.post("/api/admin/message-campaigns", json={
            "user_ids": ids[:10], "study_group_ids": [], "text": "rollback",
        }, headers=_headers(admin_init_data, key="atomic-rollback"))
    async with factory() as session:
        for table in (MessageCampaign, CampaignRecipient, ChatMessageOutbox, ChatMessage, Conversation):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
    monkeypatch.setattr(campaigns, "create_outgoing_message", original)
    good = await client.post("/api/admin/message-campaigns", json={
        "user_ids": ids[:10], "study_group_ids": [], "text": "rollback",
    }, headers=_headers(admin_init_data, key="atomic-rollback"))
    assert good.status_code == 202 and good.json()["recipient_count"] == 10


@pytest.mark.postgres
async def test_pg_revoked_admin_between_preview_and_create(
    postgres_campaign, dynamic_admin_init_data
):
    client, factory, ids, a, b = postgres_campaign
    criteria = {"user_ids": [ids[19]], "study_group_ids": [a]}
    payload = {**criteria, "text": "Привет"}
    headers = _headers(dynamic_admin_init_data, "revoked-actor")
    async with factory() as session:
        grant = AdminGrant(telegram_id=555, granted_by_telegram_id=123,
                           granted_at=datetime.now(UTC))
        session.add(grant)
        await session.commit()
        grant_id = grant.id
    assert (await client.post("/api/admin/message-campaigns/preview",
                              json=criteria, headers=headers)).status_code == 200
    async with factory() as session:
        grant = await session.get(AdminGrant, grant_id)
        grant.revoked_at = datetime.now(UTC)
        await session.commit()
    assert (await client.post("/api/admin/message-campaigns", json=payload,
                              headers=headers)).status_code == 403
    assert (await client.get("/api/admin/message-campaigns", headers=headers)).status_code == 403
    async with factory() as session:
        assert await session.scalar(select(func.count(MessageCampaign.id))) == 0
