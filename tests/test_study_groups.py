"""Study group registration, normalization, and legacy user compatibility."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from topsbottg.models import PaymentProfile, StudyGroup, User
from topsbottg.services import (
    get_or_create_user,
    get_user_study_group,
    set_user_study_group,
    start_user,
    upsert_full_name,
    upsert_payment_profile,
)
from topsbottg.study_groups import normalize_study_group_code


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("м3о-207св-25", "М3О-207СВ-25"),
        ("  М3О - 207СВ - 25  ", "М3О-207СВ-25"),
        ("M3O-207SV-25", "M3O-207SV-25"),
        ("304", "304"),
        ("М3О-207БВ-25", "М3О-207БВ-25"),
    ],
)
def test_group_normalization(raw, expected):
    assert normalize_study_group_code(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "", "  ", "M1", "М3О 207СВ 25", "М3О_207СВ_25",
        "М3О/207СВ/25", "-М3О-207", "М3О-207-", "М3О--207",
        "М3О–207СВ–25", "x" * 33, "М3О-207\nСВ-25",
        "М3О-207; DROP TABLE users",
    ],
)
def test_group_invalid_code_rejected(raw):
    with pytest.raises(ValueError, match="Введите код учебной группы"):
        normalize_study_group_code(raw)


def test_mixed_script_group_codes_are_not_silently_transliterated():
    assert normalize_study_group_code("M3O-207SV-25") != normalize_study_group_code("М3О-207СВ-25")


async def test_legacy_named_user_is_registered_without_group(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await session.commit()
        assert user.registration_completed_at is not None
        assert user.study_group_id is None
        same, needs_name = await start_user(session, 111)
        assert same.id == user.id
        assert needs_name is False


async def test_new_registration_waits_for_group_and_resumes_after_name(session_factory):
    async with session_factory() as session:
        user, needs_name = await start_user(session, 112)
        assert needs_name is True
        assert user.registration_completed_at is None
        user = await upsert_full_name(session, 112, "Петров Пётр")
        await session.commit()
        assert user.registration_completed_at is None

    async with session_factory() as session:
        resumed, needs_name = await start_user(session, 112)
        assert needs_name is False
        assert resumed.registration_completed_at is None
        group = await set_user_study_group(session, resumed, "м3о - 207св - 25", complete_registration=True)
        await session.commit()
        assert group.display_code == "М3О-207СВ-25"
        assert resumed.registration_completed_at is not None
        assert resumed.study_group_id == group.id


async def test_groups_are_unique_shared_and_editable(session_factory):
    async with session_factory() as session:
        a = await get_or_create_user(session, 200, "Иван Иванов")
        b = await get_or_create_user(session, 201, "Пётр Петров")
        group_a = await set_user_study_group(session, a, "М3О-207СВ-25")
        group_b = await set_user_study_group(session, b, "м3о - 207св - 25")
        await session.commit()
        assert group_a.id == group_b.id
        assert (await session.scalars(select(StudyGroup))).all() == [group_a]

        updated = await set_user_study_group(session, b, "М3О-208СВ-25")
        await session.commit()
        assert updated.id != group_a.id
        assert (await get_user_study_group(session, a)).display_code == "М3О-207СВ-25"
        assert (await get_user_study_group(session, b)).display_code == "М3О-208СВ-25"
        assert len((await session.scalars(select(StudyGroup))).all()) == 2


async def test_group_edit_keeps_profile_and_registration_marker(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 301, "Старый пользователь")
        await upsert_payment_profile(session, user, "СБП +79990000001")
        original_completed_at = user.registration_completed_at
        await set_user_study_group(session, user, "М3О-207СВ-25")
        await set_user_study_group(session, user, "М3О-208СВ-25")
        await session.commit()

    async with session_factory() as session:
        stored = await session.scalar(select(User).where(User.telegram_id == 301))
        profile = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == stored.id))
        assert profile.raw_payment_details == "СБП +79990000001"
        assert stored.registration_completed_at.replace(tzinfo=None) == original_completed_at.replace(tzinfo=None)
        assert (await get_user_study_group(session, stored)).display_code == "М3О-208СВ-25"


async def test_invalid_group_does_not_change_existing_group(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 303, "Старый пользователь")
        original = await set_user_study_group(session, user, "М3О-207СВ-25")
        with pytest.raises(ValueError):
            await set_user_study_group(session, user, "bad group")
        await session.commit()
        assert user.study_group_id == original.id


async def test_unregistered_placeholder_does_not_gain_registration_from_group_edit(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 304)
        assert user.registration_completed_at is None
        await set_user_study_group(session, user, "М3О-207СВ-25")
        await session.commit()
        assert user.registration_completed_at is None
