"""Конфликты изменения реквизитов при работе с PostgreSQL."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.api import create_app
from topsbottg.models import Base, PayoutRecipient, RecipientStatus
from topsbottg.services import add_recipients, create_payout, get_or_create_user


@pytest.fixture
async def postgres_api_client(settings):
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is required for real PostgreSQL API tests")

    schema = f"ci_api_{uuid4().hex}"
    admin_engine = create_async_engine(database_url)
    tenant_engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        tenant_engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with tenant_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(tenant_engine, expire_on_commit=False, class_=AsyncSession)
        app = create_app(settings.model_copy(update={"database_url": database_url}), factory)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory
    finally:
        if tenant_engine is not None:
            await tenant_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.postgres
async def test_pg_api_mark_paid_rejects_stale_snapshot_without_mutating_record(
    postgres_api_client, admin_init_data, non_admin_init_data
):
    client, factory = postgres_api_client
    async with factory() as session:
        user = await get_or_create_user(session, 987654, "Postgres User")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "period_start_day": 1,
                "period_start_month": 11,
                "period_end_day": 7,
                "period_end_month": 11,
                "message_before_period": "Test ",
                "message_after_period": "",
            },
        )
        recipient = (await add_recipients(session, payout, [user.id]))[0]
        recipient.status = RecipientStatus.payment_received.value
        recipient.payment_snapshot_version = 2
        recipient.payment_profile_snapshot = {"raw_payment_details": "new account"}
        await session.commit()
        payout_id, recipient_id = payout.id, recipient.id

    path = f"/api/admin/payouts/{payout_id}/recipients/{recipient_id}/mark-paid"

    forbidden = await client.post(
        path, headers={"X-Telegram-Init-Data": non_admin_init_data},
        json={"expected_snapshot_version": 2},
    )
    assert forbidden.status_code == 403

    stale = await client.post(
        path, headers={"X-Telegram-Init-Data": admin_init_data},
        json={"expected_snapshot_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "payment_details_changed"
    async with factory() as session:
        saved = await session.get(PayoutRecipient, recipient_id)
        assert saved is not None
        assert saved.status == RecipientStatus.payment_received.value
        assert saved.payment_snapshot_version == 2
        assert saved.payment_profile_snapshot == {"raw_payment_details": "new account"}
        assert saved.paid_at is None

    accepted = await client.post(
        path, headers={"X-Telegram-Init-Data": admin_init_data},
        json={"expected_snapshot_version": 2},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == RecipientStatus.paid.value
    async with factory() as session:
        saved = await session.get(PayoutRecipient, recipient_id)
        assert saved is not None
        assert saved.paid_by_admin_id == 123
        assert saved.payment_snapshot_version == 2
        assert saved.payment_profile_snapshot == {"raw_payment_details": "new account"}
