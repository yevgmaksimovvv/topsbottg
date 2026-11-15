"""Конкурентные приглашения и отправка уведомлений на PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from conftest import make_init_data
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.api import create_app
from topsbottg.models import AdminInvitation, Base
from topsbottg.services import get_or_create_user
from topsbottg.worker import deliver_admin_invitations


@pytest.fixture
async def postgres_invitations(settings):
    url = os.getenv("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL required")
    schema = f"ci_invites_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = None
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        app_settings = settings.model_copy(update={
            "database_url": url, "admin_telegram_ids": "123,456"
        })
        app = create_app(app_settings, factory)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory, app_settings
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.postgres
async def test_two_owners_cannot_create_two_pending_invitations(postgres_invitations):
    client, factory, settings = postgres_invitations
    async with factory() as session:
        await get_or_create_user(session, 555, "Иван Иванов")
        await session.commit()

    async def invite(owner_id):
        headers = {
            "X-Telegram-Init-Data": make_init_data(settings.bot_token, owner_id)
        }
        return await client.post(
            "/api/admin/admin-invitations",
            json={"target_telegram_id": 555},
            headers=headers,
        )

    results = await asyncio.gather(invite(123), invite(456))
    assert sorted(response.status_code for response in results) == [201, 409]
    async with factory() as session:
        invitations = list(await session.scalars(select(AdminInvitation)))
        assert len(invitations) == 1
        assert invitations[0].status == "pending"


@pytest.mark.postgres
async def test_two_workers_send_single_invitation_on_postgres(postgres_invitations):
    client, factory, settings = postgres_invitations
    async with factory() as session:
        await get_or_create_user(session, 555, "Иван Иванов")
        await session.commit()
    created = await client.post(
        "/api/admin/admin-invitations",
        json={"target_telegram_id": 555},
        headers={"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)},
    )
    assert created.status_code == 201

    class SlowBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.sent.append((chat_id, text))
            await asyncio.sleep(0.1)

    bot = SlowBot()
    await asyncio.gather(
        deliver_admin_invitations(bot, factory),
        deliver_admin_invitations(bot, factory),
    )
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 555
    async with factory() as session:
        invitation = await session.get(AdminInvitation, created.json()["id"])
        assert invitation.delivered_at is not None
