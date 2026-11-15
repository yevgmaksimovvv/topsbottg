"""Owner-facing admin directory presents names without exposing source configuration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_init_data

from topsbottg.access import grant_admin
from topsbottg.invitations import create_invitation, observe_telegram_profile
from topsbottg.services import get_or_create_user


@pytest.mark.asyncio
async def test_owner_directory_includes_all_owners_and_active_grants(
    client, settings, session_factory
):
    settings.admin_telegram_ids = "123,456"
    async with session_factory() as session:
        await get_or_create_user(session, 123, "Евгений Максимов")
        await observe_telegram_profile(session, 123, "owner_name", "Евгений", "Максимов")
        await get_or_create_user(session, 555, "Иван Иванов")
        await observe_telegram_profile(session, 555, "admin_name", "Иван", "Иванов")
        await grant_admin(
            session, actor_telegram_id=456, target_telegram_id=555,
            owner_ids=settings.admin_ids_set,
        )
        await session.commit()

    for owner_id in (123, 456):
        resp = await client.get(
            "/api/admin/admin-directory",
            headers={"X-Telegram-Init-Data": make_init_data(settings.bot_token, owner_id)},
        )
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json() == [
            {"telegram_id": 123, "role": "owner", "username": "owner_name",
             "display_name": "Евгений Максимов"},
            {"telegram_id": 456, "role": "owner", "username": None,
             "display_name": None},
            {"telegram_id": 555, "role": "admin", "username": "admin_name",
             "display_name": "Иван Иванов"},
        ]
        assert all("source" not in person for person in resp.json())

    resp = await client.get(
        "/api/admin/admin-directory",
        headers={"X-Telegram-Init-Data": make_init_data(settings.bot_token, 555)},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_directory_excludes_revoked_and_stale_usernames(client, settings, session_factory):
    from topsbottg.access import revoke_admin

    async with session_factory() as session:
        stale_user = await get_or_create_user(session, 555, "Старое имя")
        await observe_telegram_profile(session, 555, "old_handle", "Старое", "Имя")
        stale_user.telegram_profile_seen_at = datetime.now(UTC) - timedelta(days=15)
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set,
        )
        await session.commit()

    headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)}
    first = await client.get("/api/admin/admin-directory", headers=headers)
    assert first.status_code == 200
    assert first.json()[1] == {
        "telegram_id": 555, "role": "admin",
        "username": None, "display_name": "Старое имя",
    }

    async with session_factory() as session:
        assert await revoke_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set,
        )
        await session.commit()
    second = await client.get("/api/admin/admin-directory", headers=headers)
    assert len(second.json()) == 1
    assert second.json()[0]["role"] == "owner"


@pytest.mark.asyncio
async def test_invitation_list_includes_user_display_without_source(
    client, session_factory, settings
):
    async with session_factory() as session:
        await get_or_create_user(session, 555, "Иван Иванов")
        await observe_telegram_profile(session, 555, "first_handle", "Иван", "Иванов")
        invitation = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
        )
        await session.commit()
        invitation_id = invitation.id
    headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)}
    result = await client.get("/api/admin/admin-invitations", headers=headers)
    assert result.status_code == 200
    assert result.json()[0]["id"] == invitation_id
    assert result.json()[0]["username"] == "first_handle"
    assert result.json()[0]["display_name"] == "Иван Иванов"
    assert "bot_token" not in result.text
    assert "env" not in result.text
