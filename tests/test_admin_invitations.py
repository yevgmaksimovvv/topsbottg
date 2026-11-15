"""Invitation lookup, consent, retry and owner-only API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from topsbottg.access import get_principal, revoke_admin
from topsbottg.invitations import (
    InvitationConflict,
    create_invitation,
    decide_invitation,
    find_candidate,
)
from topsbottg.models import AdminInvitation, AuditLog, User
from topsbottg.services import get_or_create_user
from topsbottg.worker import deliver_admin_invitations


@pytest.fixture
def username_identity(app):
    class FakeResolver:
        def __init__(self):
            self.current = {"valid_name": 555}

        async def resolve(self, username: str) -> int | None:
            return self.current.get(username)

    resolver = FakeResolver()
    app.state.username_resolver = resolver
    return resolver


class InvitationBot:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.fail_first = fail_first

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail_first:
            self.fail_first = False
            raise OSError("temporary Telegram failure")
        self.sent.append((chat_id, text, reply_markup))


async def known(session_factory, telegram_id=555, full_name="Иван Иванов"):
    async with session_factory() as session:
        user = await get_or_create_user(session, telegram_id, full_name)
        await session.commit()
        return user.id


@pytest.mark.asyncio
async def test_candidate_lookup_only_depends_on_registered_telegram_id(session_factory):
    await known(session_factory)
    async with session_factory() as session:
        assert (await find_candidate(session, telegram_id=555)).telegram_id == 555
        assert await find_candidate(session, telegram_id=999) is None
        with pytest.raises(ValueError):
            await find_candidate(session, telegram_id=-1)


@pytest.mark.asyncio
async def test_legacy_user_can_be_invited_by_stable_telegram_id(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 777, "Старый пользователь")
        await session.commit()
    async with session_factory() as session:
        found = await find_candidate(session, telegram_id=777)
        assert found is not None and found.telegram_id == 777
        invite = await create_invitation(
            session, actor_id=123, target_id=777, owner_ids=settings.admin_ids_set
        )
        assert invite.target_telegram_id == 777
        await session.commit()
        with pytest.raises(InvitationConflict):
            await create_invitation(
                session, actor_id=123, target_id=777, owner_ids=settings.admin_ids_set,
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
async def test_username_reassignment_cannot_invite_previous_owner(
    client, session_factory, admin_init_data, username_identity
):
    await known(session_factory)
    headers = {"X-Telegram-Init-Data": admin_init_data}
    lookup = await client.get(
        "/api/admin/admin-candidates", params={"username": "valid_name"}, headers=headers,
    )
    assert lookup.status_code == 200
    assert lookup.json()[0]["telegram_id"] == 555

    username_identity.current["valid_name"] = 666
    denied = await client.post(
        "/api/admin/admin-invitations",
        json={"target_telegram_id": 555, "expected_username": "valid_name"},
        headers=headers,
    )
    assert denied.status_code == 409
    async with session_factory() as session:
        assert await session.scalar(select(AdminInvitation.id)) is None


@pytest.mark.asyncio
async def test_owner_api_lookup_invites_and_grants_only_after_acceptance(
    client, session_factory, settings, admin_init_data, username_identity
):
    await known(session_factory)
    headers = {"X-Telegram-Init-Data": admin_init_data}
    lookup = await client.get(
        "/api/admin/admin-candidates", params={"username": "@VALID_NAME"}, headers=headers
    )
    assert lookup.status_code == 200
    assert lookup.json()[0]["telegram_id"] == 555
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
    client, session_factory, admin_init_data, username_identity
):
    await known(session_factory)
    headers = {"X-Telegram-Init-Data": admin_init_data}
    username_identity.current = {"changed_name": 555, "valid_name": 666}
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
        assert invitation.last_delivery_error == "OSError"
        assert invitation.delivered_at is None
        assert invitation.delivery_state == "unknown"
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
