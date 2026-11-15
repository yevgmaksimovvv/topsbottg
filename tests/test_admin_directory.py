"""Owner-only directory, username lookup and invitation API contracts."""

from __future__ import annotations

import pytest
from conftest import make_init_data

from topsbottg.access import grant_admin, revoke_admin
from topsbottg.invitations import create_invitation
from topsbottg.services import get_or_create_user


class StubResolver:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    async def resolve(self, username: str) -> int | None:
        return self.mapping.get(username)


@pytest.mark.asyncio
async def test_admin_me_returns_role_without_mutating_profile(client, settings, session_factory):
    async with session_factory() as session:
        await get_or_create_user(session, 123, "Евгений Максимов")
        await session.commit()
    headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123, username="changed_owner")}
    response = await client.get("/api/admin/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["role"] == "owner"


@pytest.mark.asyncio
async def test_username_lookup_uses_mtproto_and_registration_db(
    client, app, settings, session_factory,
):
    settings.admin_telegram_ids = "123,456"
    async with session_factory() as session:
        await get_or_create_user(session, 123, "Первый владелец")
        await get_or_create_user(session, 456, "Второй владелец")
        await get_or_create_user(session, 555, "Иван Иванов")
        await session.commit()

    app.state.username_resolver = StubResolver({"shem2001": 456, "valid_name": 555, "other_user": 999})
    headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)}

    owner = await client.get("/api/admin/admin-candidates", params={"username": "@SHEM2001"}, headers=headers)
    assert owner.status_code == 200
    assert owner.json() == [{
        "telegram_id": 456, "username": "shem2001", "role": "owner",
        "full_name": "Второй владелец", "study_group": None,
    }]

    candidate = await client.get(
        "/api/admin/admin-candidates", params={"username": "@valid_name"}, headers=headers
    )
    assert candidate.status_code == 200
    assert candidate.json()[0]["telegram_id"] == 555
    assert candidate.json()[0]["full_name"] == "Иван Иванов"

    unknown = await client.get(
        "/api/admin/admin-candidates", params={"username": "@other_user"}, headers=headers
    )
    assert unknown.status_code == 200
    assert unknown.json() == []  # Telegram user exists, but is not registered in TOPSBOT.

    missing = await client.get(
        "/api/admin/admin-candidates", params={"username": "@no_such_user"}, headers=headers
    )
    assert missing.status_code == 200
    assert missing.json() == []


@pytest.mark.asyncio
async def test_missing_mtproto_credentials_do_not_break_numeric_id_search(
    client, app, settings, session_factory,
):
    async with session_factory() as session:
        await get_or_create_user(session, 555, "Иван Иванов")
        await session.commit()
    app.state.username_resolver = None
    headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)}
    by_id = await client.get("/api/admin/admin-candidates", params={"telegram_id": 555}, headers=headers)
    assert by_id.status_code == 200
    assert by_id.json()[0]["full_name"] == "Иван Иванов"
    assert by_id.json()[0]["username"] is None

    no_mtproto = await client.get(
        "/api/admin/admin-candidates", params={"username": "known_name"}, headers=headers
    )
    assert no_mtproto.status_code == 503
    malformed = await client.get("/api/admin/admin-candidates", params={"username": "@bad"}, headers=headers)
    assert malformed.status_code == 422

    absent = await client.get("/api/admin/admin-candidates", params={"telegram_id": 999}, headers=headers)
    assert absent.status_code == 200 and absent.json() == []


@pytest.mark.asyncio
async def test_owner_directory_includes_all_owners_and_active_grants(client, settings, session_factory):
    settings.admin_telegram_ids = "123,456"
    async with session_factory() as session:
        await get_or_create_user(session, 123, "Евгений Максимов")
        await get_or_create_user(session, 555, "Иван Иванов")
        await grant_admin(
            session, actor_telegram_id=456, target_telegram_id=555, owner_ids=settings.admin_ids_set
        )
        await session.commit()

    for owner_id in (123, 456):
        response = await client.get(
            "/api/admin/admin-directory",
            headers={"X-Telegram-Init-Data": make_init_data(settings.bot_token, owner_id)},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == [
            {"telegram_id": 123, "role": "owner", "full_name": "Евгений Максимов"},
            {"telegram_id": 456, "role": "owner", "full_name": None},
            {"telegram_id": 555, "role": "admin", "full_name": "Иван Иванов"},
        ]
        assert all("source" not in person for person in response.json())

    forbidden = await client.get(
        "/api/admin/admin-directory",
        headers={"X-Telegram-Init-Data": make_init_data(settings.bot_token, 555)},
    )
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_directory_excludes_revoked_admin(client, settings, session_factory):
    async with session_factory() as session:
        await get_or_create_user(session, 555, "Иван Иванов")
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555, owner_ids=settings.admin_ids_set
        )
        await session.commit()

    headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)}
    first = await client.get("/api/admin/admin-directory", headers=headers)
    assert first.status_code == 200
    assert first.json()[1] == {"telegram_id": 555, "role": "admin", "full_name": "Иван Иванов"}

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
async def test_invitation_list_uses_registered_name(client, session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 555, "Иван Иванов")
        invitation = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
        )
        await session.commit()
        invitation_id = invitation.id
    headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)}
    response = await client.get("/api/admin/admin-invitations", headers=headers)
    assert response.status_code == 200
    assert response.json()[0]["id"] == invitation_id
    assert response.json()[0]["full_name"] == "Иван Иванов"
    assert "bot_token" not in response.text
    assert "env" not in response.text
