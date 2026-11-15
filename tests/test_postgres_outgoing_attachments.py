"""PostgreSQL upload idempotency, claim isolation and crash recovery."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.api import create_app
from topsbottg.attachment_staging import cleanup_staging, staging_path
from topsbottg.chat_outbox import claim_chat_outbox, settle_chat_outbox
from topsbottg.models import Base, ChatAttachment, ChatMessage, ChatMessageOutbox, Conversation, Payout, User
from topsbottg.worker import deliver_chat_outbox

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


@pytest.fixture
async def postgres_attachment_client(settings):
    url = os.getenv("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL required")
    schema = f"ci_outgoing_attachment_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = None
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        app = create_app(settings.model_copy(update={"database_url": url}), factory)
        async with factory() as session:
            user = User(
                telegram_id=85101, full_name="Получатель",
                registration_completed_at=datetime.now(UTC),
            )
            session.add(user)
            await session.flush()
            conv = Conversation(user_id=user.id)
            session.add(conv)
            await session.commit()
            cid = conv.id
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory, cid
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


def _post_args(cid, headers, data=PDF):
    return {
        "url": f"/api/admin/conversations/{cid}/attachments",
        "headers": headers,
        "data": {"kind": "document", "caption": "Документ"},
        "files": {"file": ("report.pdf", data, "application/pdf")},
    }


@pytest.mark.postgres
async def test_pg_concurrent_attachment_post_reuses_message_and_single_staging(
    postgres_attachment_client, admin_init_data, settings
):
    client, factory, cid = postgres_attachment_client
    headers = {"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "same-upload"}
    responses = await asyncio.wait_for(
        asyncio.gather(*(client.post(**_post_args(cid, headers)) for _ in range(5))),
        timeout=30,
    )
    assert [response.status_code for response in responses] == [202] * 5
    ids = {response.json()["message"]["id"] for response in responses}
    assert len(ids) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count(ChatMessage.id))) == 1
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 1
        assert await session.scalar(select(func.count(ChatAttachment.chat_message_id))) == 1
        attachment = await session.scalar(select(ChatAttachment))
        assert staging_path(settings.chat_staging_dir, attachment.staging_key).read_bytes() == PDF
        assert attachment.telegram_file_id is None
    staged = [p for p in Path(settings.chat_staging_dir).iterdir() if len(p.name) == 48]
    assert len(staged) == 1

    conflicting = await asyncio.gather(
        client.post(**_post_args(cid, {**headers, "Idempotency-Key": "conflict"}, PDF)),
        client.post(**_post_args(cid, {**headers, "Idempotency-Key": "conflict"}, PDF+b"other")),
    )
    assert sorted(r.status_code for r in conflicting) == [202, 409]
    async with factory() as session:
        assert await session.scalar(select(func.count(ChatMessage.id))) == 2
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 2


@pytest.mark.postgres
async def test_pg_file_worker_reclaims_after_restart_without_open_send_transaction(
    postgres_attachment_client, admin_init_data, settings
):
    client, factory, cid = postgres_attachment_client
    request = await client.post(**_post_args(cid, {
        "X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "restart",
    }))
    assert request.status_code == 202
    message_id = request.json()["message"]["id"]
    async with factory() as session:
        [claim] = await claim_chat_outbox(session)
        await session.commit()
    path = staging_path(settings.chat_staging_dir, (
        await _get_staging_key(factory, message_id)
    ))
    assert path.is_file()
    # A new worker session sees the same job and physical file from the volume.
    async with factory() as session:
        item = await session.get(ChatMessageOutbox, claim.id)
        item.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    entered = asyncio.Event()
    release = asyncio.Event()

    class PausedBot:
        async def send_document(self, chat_id, document, **kwargs):
            assert Path(document.path).read_bytes() == PDF
            entered.set()
            await release.wait()
            return SimpleNamespace(
                message_id=935, document=SimpleNamespace(
                    file_id="private935", file_unique_id="unique935",
                ),
            )

    task = asyncio.create_task(deliver_chat_outbox(PausedBot(), factory, settings.chat_staging_dir))
    try:
        await asyncio.wait_for(entered.wait(), timeout=8)
        async with factory() as session:
            # No row lock remains while Telegram is accepting the upload.
            item = await session.scalar(select(ChatMessageOutbox).with_for_update(nowait=True))
            assert item.status == "sending" and item.attempts == 2
            await session.rollback()
            assert await cleanup_staging(settings.chat_staging_dir, factory) == 0
            assert path.exists()
    finally:
        release.set()
    await asyncio.wait_for(task, timeout=8)
    assert not path.exists()
    async with factory() as session:
        att = await session.get(ChatAttachment, message_id)
        outbox = await session.scalar(select(ChatMessageOutbox))
        assert att.available and att.telegram_file_id == "private935"
        assert outbox.status == "sent" and outbox.attempts == 2
        assert await session.scalar(select(func.count(Payout.id))) == 0


async def _get_staging_key(factory, message_id):
    async with factory() as session:
        attachment = await session.get(ChatAttachment, message_id)
        return attachment.staging_key


@pytest.mark.postgres
async def test_pg_telegram_crash_window_and_claim_token_do_not_duplicate_database_rows(
    postgres_attachment_client, admin_init_data, settings
):
    client, factory, cid = postgres_attachment_client
    upload = await client.post(**_post_args(cid, {
        "X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "crash",
    }))
    assert upload.status_code == 202
    message_id = upload.json()["message"]["id"]
    stage = staging_path(settings.chat_staging_dir, await _get_staging_key(factory, message_id))
    async with factory() as session:
        [first] = await claim_chat_outbox(session)
        await session.commit()
    # Telegram accepted #700, worker died before persisting sent.
    telegram_receipts = [700]
    async with factory() as session:
        row = await session.get(ChatMessageOutbox, first.id)
        row.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    async with factory() as session:
        [reclaimed] = await claim_chat_outbox(session)
        assert reclaimed.claim_token != first.claim_token
        await session.commit()
    assert stage.is_file()
    async with factory() as session:
        assert not await settle_chat_outbox(
            session, first, telegram_message_id=700,
            telegram_file_id="private700",
        )
        await session.commit()
    telegram_receipts.append(701)
    async with factory() as session:
        assert await settle_chat_outbox(
            session, reclaimed, telegram_message_id=701,
            telegram_file_id="private701", telegram_file_unique_id="unique701",
        )
        await session.commit()
    assert await cleanup_staging(settings.chat_staging_dir, factory) == 1
    assert not stage.exists()
    async with factory() as session:
        assert await session.scalar(select(func.count(ChatMessage.id))) == 1
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 1
        attachment = await session.get(ChatAttachment, message_id)
        assert attachment.telegram_file_id == "private701" and attachment.available
    assert telegram_receipts == [700, 701]


@pytest.mark.postgres
async def test_pg_slow_media_upload_cannot_be_claimed_while_sending(
    postgres_attachment_client, settings, admin_init_data, monkeypatch
):
    import topsbottg.worker as worker
    from topsbottg.chat_outbox import CHAT_MEDIA_CLAIM_LEASE, CHAT_MEDIA_SEND_TIMEOUT_SECONDS

    client, factory, cid = postgres_attachment_client
    response = await client.post(**_post_args(cid, {
        "X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "pg-slow-file",
    }))
    assert response.status_code == 202
    monkeypatch.setattr(worker, "CHAT_SEND_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(worker, "CHAT_MEDIA_SEND_TIMEOUT_SECONDS", 0.25)
    entered = asyncio.Event()
    release = asyncio.Event()

    class DelayedBot:
        async def send_document(self, chat_id, document, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(
                message_id=903, document=SimpleNamespace(
                    file_id="private903", file_unique_id="unique903",
                ),
            )

    task = asyncio.create_task(deliver_chat_outbox(DelayedBot(), factory, settings.chat_staging_dir))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        async with factory() as session:
            current = await session.scalar(select(ChatMessageOutbox))
            assert current.attempts == 1 and current.status == "sending"
            assert current.claimed_until.replace(tzinfo=UTC) > (
                datetime.now(UTC) + CHAT_MEDIA_CLAIM_LEASE - timedelta(seconds=2)
            )
            # Simulate the full 300-second send budget passing on the claim clock.
            waiting = await asyncio.wait_for(
                claim_chat_outbox(
                    session, now=datetime.now(UTC) + timedelta(seconds=CHAT_MEDIA_SEND_TIMEOUT_SECONDS)
                ),
                timeout=3,
            )
            assert waiting == []
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=4)
    async with factory() as session:
        row = await session.scalar(select(ChatMessageOutbox))
        assert row.status == "sent" and row.attempts == 1
        att = await session.scalar(select(ChatAttachment))
        assert att.staging_key is None and att.telegram_file_id == "private903"
