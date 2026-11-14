"""Admin study group endpoints preserve search, pagination and access rules."""

from __future__ import annotations

import pytest

from topsbottg.models import StudyGroup
from topsbottg.services import (
    get_or_create_user,
    set_user_study_group,
    upsert_payment_profile,
)


@pytest.mark.asyncio
async def test_admin_study_groups_lists_only_assigned_groups(
    client, session_factory, admin_init_data
):
    headers = {"X-Telegram-Init-Data": admin_init_data}
    empty = await client.get("/api/admin/study-groups", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == []
    assert empty.headers["cache-control"] == "no-store"

    async with session_factory() as session:
        first = await get_or_create_user(session, 111, "Иван Иванов")
        second = await get_or_create_user(session, 222, "Пётр Петров")
        group_b = await set_user_study_group(session, first, "М3О-208СВ-25")
        group_a = await set_user_study_group(session, second, "М3О-207СВ-25")
        await session.flush()
        session.add(StudyGroup(display_code="М3О-209СВ-25", normalized_code="М3О-209СВ-25"))
        await session.commit()

    groups = await client.get("/api/admin/study-groups", headers=headers)
    assert groups.status_code == 200
    assert groups.json() == [
        {"id": group_a.id, "code": "М3О-207СВ-25"},
        {"id": group_b.id, "code": "М3О-208СВ-25"},
    ]


@pytest.mark.asyncio
async def test_users_include_study_group_and_legacy_null(
    client, session_factory, admin_init_data
):
    headers = {"X-Telegram-Init-Data": admin_init_data}
    async with session_factory() as session:
        named = await get_or_create_user(session, 111, "Иван Иванов")
        old = await get_or_create_user(session, 222, "Пётр Петров")
        group = await set_user_study_group(session, named, "М3О-207СВ-25")
        await session.commit()
        named_id = named.id
        old_id = old.id
        group_id = group.id

    result = await client.get("/api/admin/users", headers=headers)
    assert result.status_code == 200
    users = {item["id"]: item for item in result.json()["items"]}
    assert users[named_id]["study_group"] == {"id": group_id, "code": "М3О-207СВ-25"}
    assert users[old_id]["study_group"] is None

    detail = await client.get(f"/api/admin/users/{named_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["user"]["study_group"] == {
        "id": group_id, "code": "М3О-207СВ-25"
    }


@pytest.mark.asyncio
async def test_users_group_filter_combines_search_profile_and_pagination(
    client, session_factory, admin_init_data
):
    headers = {"X-Telegram-Init-Data": admin_init_data}
    async with session_factory() as session:
        group_id = None
        for telegram_id, name, code, payable in [
            (111, "Иван Иванов", "М3О-207СВ-25", True),
            (222, "Иван Петров", "М3О-207СВ-25", True),
            (333, "Мария Петрова", "М3О-207СВ-25", False),
            (444, "Иван Сидоров", "М3О-208СВ-25", True),
            (555, "Иван Без Группы", None, False),
        ]:
            user = await get_or_create_user(session, telegram_id, name)
            if code:
                group = await set_user_study_group(session, user, code)
                if code == "М3О-207СВ-25":
                    group_id = group.id
            if payable:
                await upsert_payment_profile(session, user, "СБП +79990000000")
        await session.commit()

    params = {
        "study_group_id": group_id,
        "search": "Иван",
        "has_payment_profile": "true",
        "limit": 1,
        "offset": 0,
    }
    first = await client.get("/api/admin/users", params=params, headers=headers)
    assert first.status_code == 200
    assert first.json()["has_more"] is True
    assert [x["full_name"] for x in first.json()["items"]] == ["Иван Иванов"]

    second = await client.get(
        "/api/admin/users", params={**params, "offset": 1}, headers=headers
    )
    assert second.status_code == 200
    assert [x["full_name"] for x in second.json()["items"]] == ["Иван Петров"]
    assert second.json()["has_more"] is False

    empty = await client.get(
        "/api/admin/users",
        params={"study_group_id": group_id, "search": "Несуществующий"},
        headers=headers,
    )
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["has_more"] is False

    all_users = await client.get("/api/admin/users", headers=headers)
    assert len(all_users.json()["items"]) == 5


@pytest.mark.asyncio
async def test_invalid_group_filter_is_rejected(client, admin_init_data):
    headers = {"X-Telegram-Init-Data": admin_init_data}
    for invalid in ("0", "-1", "wrong"):
        result = await client.get(
            "/api/admin/users", params={"study_group_id": invalid}, headers=headers
        )
        assert result.status_code == 422
