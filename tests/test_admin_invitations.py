"""Invitation lookup, consent, retry and owner-only API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import topsbottg.api as api_module
from topsbottg.access import get_principal, revoke_admin
from topsbottg.invitations import (
    InvitationConflict,
    create_invitation,
    decide_invitation,
    find_candidate,
    observe_telegram_profile,
)
from topsbottg.models import AdminInvitation, AuditLog, User
from topsbottg.services import get_or_create_user
from topsbottg.worker import deliver_admin_invitations


@pytest.fixture(autouse=True)
def telegram_identity(monkeypatch):
    current = {"username": "valid_name"}

    class FakeBot:
        def __init__(self, token):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get_chat(self, telegram_id):
            return SimpleNamespace(username=current["username"])

    monkeypatch.setattr(api_module, "Bot", FakeBot)
    return current


class InvitationBot:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.fail_first = fail_first

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail_first:
            self.fail_first = False
            raise OSError("temporary Telegram failure")
        self.sent.append((chat_id, text, reply_markup))


async def known(session_factory, telegram_id=555, username="valid_name", full_name="Иван Иванов"):
    async with session_factory() as session:
        user = await get_or_create_user(session, telegram_id, full_name)
        await observe_telegram_profile(session, telegram_id, username, "Иван", "Иванов")
        await session.commit()
        return user.id


@pytest.mark.asyncio
async def test_lookup_uses_recent_observed_identity_not_global_telegram_search(session_factory):
    await known(session_factory)
    async with session_factory() as session:
        first = await find_candidate(session, username="@VALID_name")
        assert first is not None and first.telegram_id == 555
        assert (await find_candidate(session, telegram_id=555)).telegram_id == 555
        assert await find_candidate(session, username="@missing_person") is None
        assert await find_candidate(session, telegram_id=999) is None
        with pytest.raises(ValueError):
            await find_candidate(session)
        with pytest.raises(ValueError):
            await find_candidate(session, username="@a")
        await observe_telegram_profile(session, 555, "new_handle", "Ivan", "New")
        await session.commit()
        assert await find_candidate(session, username="valid_name") is None
        assert (await find_candidate(session, username="new_handle")).telegram_id == 555
        user = await session.scalar(select(User).where(User.telegram_id == 555))
        user.telegram_profile_seen_at = datetime.now(UTC) - timedelta(days=15)
        await session.commit()
        assert (await find_candidate(session, telegram_id=555)).telegram_id == 555
        # A stale username can be found; the live Telegram identity is checked at invite time.
        assert (await find_candidate(session, username="new_handle")).telegram_id == 555
        invite = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids={123},
            expected_username="new_handle",
        )
        assert invite.target_telegram_id == 555


@pytest.mark.asyncio
async def test_legacy_user_can_be_invited_by_stable_telegram_id(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 777, "Старый пользователь")
        await session.commit()
    async with session_factory() as session:
        found = await find_candidate(session, telegram_id=777)
        assert found is not None and found.telegram_username is None
        invite = await create_invitation(
            session, actor_id=123, target_id=777, owner_ids=settings.admin_ids_set
        )
        assert invite.target_telegram_id == 777
        await session.commit()
        with pytest.raises(InvitationConflict):
            await create_invitation(
                session, actor_id=123, target_id=777, owner_ids=settings.admin_ids_set,
                expected_username="unverified_name",
            )


@pytest.mark.asyncio
async def test_invitation_remains_valid_if_inviting_owner_loses_access(session_factory, settings):
    await known(session_factory)
    async with session_factory() as session:
        invite = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids={123}
        )
        await session.commit()
    async with session_factory() as session:
        outcome = await decide_invitation(
            session, invitation_id=invite.id, actor_id=555,
            accept=True, owner_ids={456},
        )
        await session.commit()
        assert outcome == "accepted"
        assert (await get_principal(session, 555, {456})).role == "admin"


@pytest.mark.asyncio
async def test_reused_username_is_rejected_instead_of_guessing_target(session_factory):
    await known(session_factory, 555, "reused_name", "Первый")
    await known(session_factory, 666, "reused_name", "Второй")
    async with session_factory() as session:
        with pytest.raises(InvitationConflict):
            await find_candidate(session, username="reused_name")


@pytest.mark.asyncio
async def test_owner_api_lookup_invites_and_grants_only_after_acceptance(
    client, session_factory, settings, admin_init_data
):
    await known(session_factory)
    headers = {"X-Telegram-Init-Data": admin_init_data}
    lookup = await client.get(
        "/api/admin/admin-candidates", params={"username": "@VALID_NAME"}, headers=headers
    )
    assert lookup.status_code == 200
    assert lookup.json()[0]["telegram_id"] == 555
    assert lookup.json()[0]["display_name"] == "Иван Иванов"
    assert lookup.json()[0]["full_name"] == "Иван Иванов"

    created = await client.post(
        "/api/admin/admin-invitations",
        json={"target_telegram_id": 555, "expected_username": "valid_name"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    invitation_id = created.json()["id"]
    assert created.json()["status"] == "pending"
    async with session_factory() as session:
        assert await get_principal(session, 555, settings.admin_ids_set) is None

    duplicate = await client.post(
        "/api/admin/admin-invitations",
        json={"target_telegram_id": 555},
        headers=headers,
    )
    assert duplicate.status_code == 409

    bot = InvitationBot()
    await deliver_admin_invitations(bot, session_factory)
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 555
    markup = bot.sent[0][2]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        f"admin_invite:accept:{invitation_id}", f"admin_invite:decline:{invitation_id}"
    ]
    await deliver_admin_invitations(bot, session_factory)
    assert len(bot.sent) == 1

    async with session_factory() as session:
        with pytest.raises(PermissionError):
            await decide_invitation(
                session, invitation_id=invitation_id, actor_id=999, accept=True,
                owner_ids=settings.admin_ids_set
            )
        assert (await decide_invitation(
            session, invitation_id=invitation_id, actor_id=555, accept=True,
            owner_ids=settings.admin_ids_set
        )) == "accepted"
        assert (await decide_invitation(
            session, invitation_id=invitation_id, actor_id=555, accept=True,
            owner_ids=settings.admin_ids_set
        )) == "accepted"
        await session.commit()
    async with session_factory() as session:
        assert (await get_principal(session, 555, settings.admin_ids_set)).role == "admin"
        actions = list(await session.scalars(select(AuditLog.action).order_by(AuditLog.id)))
        assert actions.count("admin_granted") == 1
        assert actions.count("admin_invitation_accepted") == 1
    listing = await client.get("/api/admin/admin-invitations", headers=headers)
    assert listing.status_code == 200
    assert listing.json()[0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_username_change_blocks_stale_invitation_and_unknown_identity(
    client, session_factory, admin_init_data, telegram_identity
):
    telegram_identity["username"] = "changed_name"
    await known(session_factory)
    headers = {"X-Telegram-Init-Data": admin_init_data}
    async with session_factory() as session:
        await observe_telegram_profile(session, 555, "changed_name", "Иван", "Иванов")
        await session.commit()
    bad = await client.post(
        "/api/admin/admin-invitations",
        json={"target_telegram_id": 555, "expected_username": "valid_name"},
        headers=headers,
    )
    assert bad.status_code == 409
    absent = await client.post(
        "/api/admin/admin-invitations", json={"target_telegram_id": 999}, headers=headers
    )
    assert absent.status_code == 409
    okay = await client.post(
        "/api/admin/admin-invitations",
        json={"target_telegram_id": 555, "expected_username": "@changed_name"},
        headers=headers,
    )
    assert okay.status_code == 201


@pytest.mark.asyncio
async def test_owner_can_cancel_pending_invitation_before_delivery(
    client, session_factory, settings, admin_init_data
):
    await known(session_factory)
    headers = {"X-Telegram-Init-Data": admin_init_data}
    result = await client.post(
        "/api/admin/admin-invitations", json={"target_telegram_id": 555}, headers=headers
    )
    invitation_id = result.json()["id"]
    cancelled = await client.post(
        f"/api/admin/admin-invitations/{invitation_id}/cancel", headers=headers
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    bot = InvitationBot()
    await deliver_admin_invitations(bot, session_factory)
    assert not bot.sent
    async with session_factory() as session:
        assert await get_principal(session, 555, settings.admin_ids_set) is None
        assert await decide_invitation(
            session, invitation_id=invitation_id, actor_id=555, accept=True,
            owner_ids=settings.admin_ids_set
        ) == "cancelled"


@pytest.mark.asyncio
async def test_decline_expiration_reinvite_and_regrant(session_factory, settings):
    await known(session_factory)
    async with session_factory() as session:
        first = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
        )
        await session.commit()
        assert await decide_invitation(
            session, invitation_id=first.id, actor_id=555, accept=False,
            owner_ids=settings.admin_ids_set
        ) == "declined"
        assert await get_principal(session, 555, settings.admin_ids_set) is None
        second = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
        )
        second.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        assert await decide_invitation(
            session, invitation_id=second.id, actor_id=555, accept=True,
            owner_ids=settings.admin_ids_set
        ) == "expired"
        third = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
        )
        assert await decide_invitation(
            session, invitation_id=third.id, actor_id=555, accept=True,
            owner_ids=settings.admin_ids_set
        ) == "accepted"
        await session.commit()
        assert await get_principal(session, 555, settings.admin_ids_set) is not None
        with pytest.raises(InvitationConflict):
            await create_invitation(
                session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
            )
        assert await revoke_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        fourth = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
        )
        assert fourth.id > third.id
        await session.commit()


@pytest.mark.asyncio
async def test_uncertain_invitation_delivery_is_not_retried(session_factory, settings):
    await known(session_factory)
    async with session_factory() as session:
        invitation = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
        )
        id_ = invitation.id
        await session.commit()
    bot = InvitationBot(fail_first=True)
    await deliver_admin_invitations(bot, session_factory)
    async with session_factory() as session:
        invitation = await session.get(AdminInvitation, id_)
        assert invitation.delivery_attempts == 1
        assert invitation.last_delivery_error == "OSError"
        assert invitation.delivered_at is None
        assert invitation.delivery_state == "unknown"
        invitation.next_delivery_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    await deliver_admin_invitations(bot, session_factory)
    assert len(bot.sent) == 0
    async with session_factory() as session:
        invitation = await session.get(AdminInvitation, id_)
        assert invitation.delivered_at is None
        assert invitation.delivery_state == "unknown"
        assert invitation.last_delivery_error == "OSError"
        assert await get_principal(session, 555, settings.admin_ids_set) is None


@pytest.mark.asyncio
async def test_inflight_invitation_is_not_resent_after_worker_restart(session_factory, settings):
    await known(session_factory)
    async with session_factory() as session:
        invitation = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
        )
        invitation.delivery_state = "sending"
        invitation.updated_at = datetime.now(UTC) - timedelta(minutes=3)
        invitation_id = invitation.id
        await session.commit()

    bot = InvitationBot()
    await deliver_admin_invitations(bot, session_factory)
    await deliver_admin_invitations(bot, session_factory)
    assert bot.sent == []
    async with session_factory() as session:
        invitation = await session.get(AdminInvitation, invitation_id)
        assert invitation.delivery_state == "unknown"
        assert invitation.last_delivery_error == "delivery_outcome_unknown"


@pytest.mark.asyncio
async def test_other_owner_can_invite_and_revoke_and_admin_cannot(session_factory, settings):
    settings.admin_telegram_ids = "123,456"
    await known(session_factory)
    async with session_factory() as session:
        with pytest.raises(PermissionError):
            await create_invitation(
                session, actor_id=999, target_id=555, owner_ids=settings.admin_ids_set
            )
        with pytest.raises(InvitationConflict):
            await create_invitation(
                session, actor_id=123, target_id=456, owner_ids=settings.admin_ids_set
            )
        invited = await create_invitation(
            session, actor_id=456, target_id=555, owner_ids=settings.admin_ids_set
        )
        assert invited.created_by_owner_id == 456
        assert await decide_invitation(
            session, invitation_id=invited.id, actor_id=555,
            accept=True, owner_ids=settings.admin_ids_set
        ) == "accepted"
        await session.commit()
        assert (await get_principal(session, 555, settings.admin_ids_set)).role == "admin"
        assert await revoke_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
