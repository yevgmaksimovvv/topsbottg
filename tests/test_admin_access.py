"""Проверки административных ролей, грантов и SSE."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from topsbottg.access import (
    AccessDenied,
    get_principal,
    grant_admin,
    require_admin,
    require_owner,
    revoke_admin,
)
from topsbottg.models import AdminGrant, AuditLog
from topsbottg.services import publish_admin_event


@pytest.mark.asyncio
async def test_two_environment_owners_are_equal_without_user_rows(session_factory, settings):
    settings.admin_telegram_ids = "123,456"
    async with session_factory() as session:
        first = await require_admin(session, 123, settings.admin_ids_set)
        second = await require_admin(session, 456, settings.admin_ids_set)
        assert first.role == second.role == "owner"
        assert require_owner(456, settings.admin_ids_set).is_owner

        # Even a revoked database row cannot override an owner from the environment.
        session.add(
            AdminGrant(
                telegram_id=456, granted_by_telegram_id=123,
                granted_at=datetime.now(UTC), revoked_at=datetime.now(UTC)
            )
        )
        await session.commit()
        assert (await get_principal(session, 456, settings.admin_ids_set)).role == "owner"
        with pytest.raises(ValueError, match="owner"):
            await revoke_admin(
                session, actor_telegram_id=123, target_telegram_id=456,
                owner_ids=settings.admin_ids_set
            )


@pytest.mark.asyncio
async def test_grant_revoke_regrant_audits_history_and_does_not_touch_users(
    session_factory, settings
):
    async with session_factory() as session:
        assert await get_principal(session, 555, settings.admin_ids_set) is None
        with pytest.raises(AccessDenied):
            await grant_admin(
                session, actor_telegram_id=555, target_telegram_id=777,
                owner_ids=settings.admin_ids_set
            )
        with pytest.raises(AccessDenied):
            await revoke_admin(
                session, actor_telegram_id=555, target_telegram_id=777,
                owner_ids=settings.admin_ids_set
            )
        assert await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        assert not await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        assert (await get_principal(session, 555, settings.admin_ids_set)).role == "admin"
        assert await revoke_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        assert not await revoke_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        assert await get_principal(session, 555, settings.admin_ids_set) is None
        assert await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        await session.commit()

    async with session_factory() as session:
        assert (await get_principal(session, 555, settings.admin_ids_set)).role == "admin"
        assert await session.scalar(select(AdminGrant.telegram_id)) == 555
        audit = list(await session.scalars(select(AuditLog.action).order_by(AuditLog.id)))
        assert audit == ["admin_granted", "admin_revoked", "admin_granted"]


@pytest.mark.asyncio
async def test_grant_is_independent_from_user_registration(session_factory, settings):
    async with session_factory() as session:
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        await session.commit()
    async with session_factory() as session:
        # No User(telegram_id=555) is ever created.
        assert (await get_principal(session, 555, settings.admin_ids_set)).role == "admin"


@pytest.mark.asyncio
async def test_database_failure_cannot_authorize_dynamic_admin(settings):
    class BrokenSession:
        async def scalar(self, _statement):
            raise OperationalError("SELECT ...", {}, RuntimeError("database down"))

    with pytest.raises(OperationalError):
        await require_admin(BrokenSession(), 555, settings.admin_ids_set)
    # Owners resolve before making any database calls.
    assert (await get_principal(BrokenSession(), 123, settings.admin_ids_set)).role == "owner"


@pytest.mark.asyncio
async def test_owner_only_admin_grants_endpoints(
    client, session_factory, settings, admin_init_data, dynamic_admin_init_data
):
    owner_headers = {"X-Telegram-Init-Data": admin_init_data}
    admin_headers = {"X-Telegram-Init-Data": dynamic_admin_init_data}
    async with session_factory() as session:
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        await session.commit()
    for path in ("/api/admin/admin-grants", "/api/admin/admin-grants/555"):
        if path.endswith("555"):
            response = await client.delete(path, headers=admin_headers)
        else:
            response = await client.get(path, headers=admin_headers)
        assert response.status_code == 403

    listing = await client.get("/api/admin/admin-grants", headers=owner_headers)
    assert listing.status_code == 200
    assert [item["telegram_id"] for item in listing.json()] == [555]
    forbidden_owner = await client.delete("/api/admin/admin-grants/123", headers=owner_headers)
    assert forbidden_owner.status_code == 400
    assert (await client.delete("/api/admin/admin-grants/555", headers=owner_headers)).status_code == 204
    assert (await client.get("/api/admin/me", headers=admin_headers)).status_code == 403


@pytest.mark.asyncio
async def test_dynamic_admin_can_use_payout_api_and_get_role(
    client, session_factory, settings, admin_init_data, dynamic_admin_init_data
):
    async with session_factory() as session:
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        await session.commit()
    for init_data, expected_role in [
        (admin_init_data, "owner"), (dynamic_admin_init_data, "admin")
    ]:
        headers = {"X-Telegram-Init-Data": init_data}
        me = await client.get("/api/admin/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["role"] == expected_role
        assert me.json()["is_admin"] is True
        assert (await client.get("/api/admin/payouts", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_sse_token_bound_to_user_and_rejected_after_revocation(
    client, app, session_factory, settings, dynamic_admin_init_data
):
    headers = {"X-Telegram-Init-Data": dynamic_admin_init_data}
    async with session_factory() as session:
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        await session.commit()
    token_response = await client.post("/api/admin/events-token", headers=headers)
    assert token_response.status_code == 200
    token = token_response.cookies["topsbottg_events"]
    async with session_factory() as session:
        await revoke_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        await session.commit()
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/admin/events")
    with pytest.raises(Exception) as raised:
        await route.endpoint(token=token)
    assert getattr(raised.value, "status_code", None) == 403
    assert (await client.post("/api/admin/events-token", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_sse_stream_stops_before_emitting_after_revocation(
    client, app, session_factory, settings, dynamic_admin_init_data
):
    headers = {"X-Telegram-Init-Data": dynamic_admin_init_data}
    async with session_factory() as session:
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        await session.commit()

    token = (await client.post("/api/admin/events-token", headers=headers)).cookies["topsbottg_events"]
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/admin/events")
    response = await route.endpoint(token=token)
    stream = response.body_iterator
    assert "event: ping" in await anext(stream)
    try:
        async with session_factory() as session:
            await revoke_admin(
                session, actor_telegram_id=123, target_telegram_id=555,
                owner_ids=settings.admin_ids_set
            )
            await session.commit()
        publish_admin_event("users_changed", {"user_id": 999})
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await stream.aclose()
