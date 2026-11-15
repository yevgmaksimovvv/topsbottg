"""Owner directory and invitation candidate search contracts."""

from __future__ import annotations

import pytest
from conftest import make_init_data

from topsbottg.access import grant_admin, revoke_admin
from topsbottg.invitations import create_invitation, decide_invitation
from topsbottg.models import StudyGroup
from topsbottg.services import get_or_create_user


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
async def test_name_search_includes_people_without_group_and_can_invite_them(
    client, settings, session_factory,
):
    async with session_factory() as session:
        group = StudyGroup(display_code="М3О-207СВ-25", normalized_code="М3О-207СВ-25")
        session.add(group)
        await session.flush()
        with_group = await get_or_create_user(session, 555, "Иванов Иван Иванович")
        with_group.study_group_id = group.id
        await get_or_create_user(session, 666, "Иванов Иван Иванович")
        await get_or_create_user(session, 777, "Иванов Пётр Сергеевич")
        await get_or_create_user(session, 888)  # Unfinished registration, no FIO.
        await session.commit()

    headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)}
    result = await client.get("/api/admin/admin-candidates", params={"search": "Иванов"}, headers=headers)
    assert result.status_code == 200, result.text
    assert [row["telegram_id"] for row in result.json()["items"]] == [555, 666, 777]
    assert result.json()["has_more"] is False
    assert result.json()["items"][0]["study_group"] == "М3О-207СВ-25"
    assert result.json()["items"][1]["study_group"] is None
    assert all(row["role"] is None for row in result.json()["items"])
    assert all("raw_payment_details" not in row for row in result.json()["items"])

    by_parts = await client.get(
        "/api/admin/admin-candidates", params={"search": "Иван Иванов"}, headers=headers,
    )
    assert by_parts.status_code == 200
    assert [row["telegram_id"] for row in by_parts.json()["items"]] == [555, 666]

    filtered = await client.get(
        "/api/admin/admin-candidates",
        params={"search": "Иванов", "study_group_id": group.id}, headers=headers,
    )
    assert filtered.status_code == 200
    assert [row["telegram_id"] for row in filtered.json()["items"]] == [555]

    # The group filter is only for searching. A person without a group may be invited.
    invited = await client.post(
        "/api/admin/admin-invitations",
        json={"target_telegram_id": 666}, headers=headers,
    )
    assert invited.status_code == 201, invited.text
    async with session_factory() as session:
        assert await decide_invitation(
            session, invitation_id=invited.json()["id"], actor_id=666,
            accept=True, owner_ids=settings.admin_ids_set,
        ) == "accepted"
        await session.commit()


@pytest.mark.asyncio
async def test_exact_telegram_id_lookup_works_without_name_or_group(
    client, settings, session_factory,
):
    async with session_factory() as session:
        await get_or_create_user(session, 555)
        await session.commit()
    headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)}
    for params in ({"search": "555"}, {"telegram_id": 555, "study_group_id": 999}):
        response = await client.get("/api/admin/admin-candidates", params=params, headers=headers)
        assert response.status_code == 200
        assert response.json() == {
            "items": [{"telegram_id": 555, "full_name": None, "study_group": None, "role": None}],
            "has_more": False,
        }
    unknown = await client.get(
        "/api/admin/admin-candidates", params={"search": "999"}, headers=headers,
    )
    assert unknown.status_code == 200 and unknown.json()["items"] == []


@pytest.mark.asyncio
async def test_name_lookup_is_paginated_and_rejects_unbounded_queries(
    client, settings, session_factory,
):
    async with session_factory() as session:
        for telegram_id in (555, 556, 557):
            await get_or_create_user(session, telegram_id, "Петров Иван")
        await session.commit()
    headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)}
    first = await client.get(
        "/api/admin/admin-candidates", params={"search": "Петров", "limit": 2}, headers=headers,
    )
    assert first.status_code == 200
    assert [row["telegram_id"] for row in first.json()["items"]] == [555, 556]
    assert first.json()["has_more"] is True
    second = await client.get(
        "/api/admin/admin-candidates",
        params={"search": "Петров", "limit": 2, "offset": 2}, headers=headers,
    )
    assert [row["telegram_id"] for row in second.json()["items"]] == [557]
    assert second.json()["has_more"] is False

    for params in ({"search": " "}, {"search": "П"}, {"search": "0"},
                   {"search": "9999999999999999999999999"}, {},
                   {"search": "Петров", "telegram_id": 555}, {"search": "Петров", "limit": 51}):
        invalid = await client.get("/api/admin/admin-candidates", params=params, headers=headers)
        assert invalid.status_code == 422, (params, invalid.text)
    wildcard = await client.get(
        "/api/admin/admin-candidates", params={"search": "%%"}, headers=headers,
    )
    assert wildcard.status_code == 200 and wildcard.json()["items"] == []


@pytest.mark.asyncio
async def test_candidate_search_is_owner_only_and_shows_existing_roles(
    client, settings, session_factory,
):
    settings.admin_telegram_ids = "123,456"
    async with session_factory() as session:
        await get_or_create_user(session, 456, "Иванов Владелец")
        await get_or_create_user(session, 555, "Иванов Администратор")
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555, owner_ids=settings.admin_ids_set,
        )
        await session.commit()
    owner_headers = {"X-Telegram-Init-Data": make_init_data(settings.bot_token, 123)}
    result = await client.get(
        "/api/admin/admin-candidates", params={"search": "Иванов"}, headers=owner_headers,
    )
    assert result.status_code == 200
    assert {row["telegram_id"]: row["role"] for row in result.json()["items"]} == {
        456: "owner", 555: "admin",
    }
    forbidden = await client.get(
        "/api/admin/admin-candidates", params={"search": "Иванов"},
        headers={"X-Telegram-Init-Data": make_init_data(settings.bot_token, 555)},
    )
    assert forbidden.status_code == 403


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
