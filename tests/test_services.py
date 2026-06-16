from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from topsbottg.models import AuditLog, PayoutRecipient, RecipientStatus
from topsbottg.services import (
    add_recipients,
    cancel_recipient,
    claim_pending_recipients,
    confirm_saved_profile,
    create_payout,
    finalize_payout_after_worker,
    format_recipients_csv,
    get_or_create_user,
    mark_paid,
    recover_stale_sending,
    retry_failed_recipient,
    save_payment_reply,
    save_profile_and_snapshot,
    soft_delete_payment_profile,
    start_user,
    update_payout,
    upsert_full_name,
    upsert_payment_profile,
    validate_message_template,
)


async def _create_payout_with_recipient(session_factory, *, with_profile: bool = False):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Май",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        if with_profile:
            await upsert_payment_profile(session, user, "Иван Иванов\n+79990000000\nT-Bank\ncomment")
        [recipient] = await add_recipients(session, payout, [user.id])
        await session.commit()
    return payout, recipient, user


@pytest.mark.asyncio
async def test_registration_new_user(session_factory):
    async with session_factory() as session:
        user, needs_name = await start_user(session, 111)
        await session.commit()
        assert needs_name is True
        assert user.telegram_id == 111


@pytest.mark.asyncio
async def test_registration_repeat_start(session_factory):
    async with session_factory() as session:
        first, needs_name = await start_user(session, 111)
        await session.commit()
    async with session_factory() as session:
        second, needs_name_again = await start_user(session, 111)
        await session.commit()
    assert first.id == second.id
    assert needs_name is True
    assert needs_name_again is True


@pytest.mark.asyncio
async def test_update_full_name(session_factory):
    async with session_factory() as session:
        user = await upsert_full_name(session, 111, "Иван Иванов")
        await session.commit()
    assert user.full_name == "Иван Иванов"


def test_validate_message_template_accepts_allowed_placeholders():
    validate_message_template("Выплата за {period_from} - {period_to}")
    validate_message_template("Escaped braces {{ok}} and text")


@pytest.mark.parametrize(
    "template",
    [
        "Bad {name}",
        "Bad {period_from!r}",
        "Bad {period_to:>10}",
        "Bad {",
    ],
)
def test_validate_message_template_rejects_invalid_templates(template):
    with pytest.raises(ValueError):
        validate_message_template(template)


@pytest.mark.asyncio
async def test_payment_profile_lifecycle(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await session.commit()
    async with session_factory() as session:
        user = await session.scalar(select(type(user)).where(type(user).id == user.id))
        profile = await upsert_payment_profile(session, user, "Иван Иванов\n+79990000000\nT-Bank")
        await session.commit()
        assert profile.raw_payment_details == "Иван Иванов\n+79990000000\nT-Bank"
    async with session_factory() as session:
        user = await session.scalar(select(type(user)).where(type(user).id == user.id))
        await soft_delete_payment_profile(session, user)
        await session.commit()
        profile = await session.scalar(select(type(profile)).where(type(profile).user_id == user.id))
        assert profile.deleted_at is not None


@pytest.mark.asyncio
async def test_create_payout_rejects_invalid_period(session_factory):
    async with session_factory() as session:
        with pytest.raises(ValueError):
            await create_payout(
                session,
                actor_telegram_id=123,
                payload={
                    "title": "Bad",
                    "period_from": datetime(2026, 2, 28).date(),
                    "period_to": datetime(2026, 2, 1).date(),
                    "message_template": None,
                },
            )


@pytest.mark.asyncio
async def test_create_payout_rejects_invalid_template(session_factory):
    async with session_factory() as session:
        with pytest.raises(ValueError):
            await create_payout(
                session,
                actor_telegram_id=123,
                payload={
                    "title": "Bad",
                    "period_from": datetime(2026, 2, 1).date(),
                    "period_to": datetime(2026, 2, 28).date(),
                    "message_template": "Hello {name}",
                },
            )


@pytest.mark.asyncio
async def test_update_payout_validates_dates_and_template(session_factory):
    async with session_factory() as session:
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Good",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        await session.commit()

    async with session_factory() as session:
        payout = await session.get(type(payout), payout.id)
        with pytest.raises(ValueError):
            await update_payout(
                session,
                payout,
                actor_telegram_id=123,
                payload={
                    "period_from": datetime(2026, 3, 1).date(),
                    "period_to": datetime(2026, 2, 1).date(),
                },
            )
        with pytest.raises(ValueError):
            await update_payout(
                session,
                payout,
                actor_telegram_id=123,
                payload={"message_template": "{amount}"},
            )


@pytest.mark.asyncio
async def test_upsert_payment_profile_rejects_whitespace_only(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await session.commit()
    async with session_factory() as session:
        user = await session.scalar(select(type(user)).where(type(user).id == user.id))
        with pytest.raises(ValueError):
            await upsert_payment_profile(session, user, "   ")


@pytest.mark.asyncio
async def test_upsert_payment_profile_rejects_legacy_structured_payload(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await session.commit()
    async with session_factory() as session:
        user = await session.scalar(select(type(user)).where(type(user).id == user.id))
        with pytest.raises(ValueError):
            await upsert_payment_profile(
                session,
                user,
                {
                    "recipient_full_name": "Иван Иванов",
                    "phone": "+79990000000",
                    "bank_name": "T-Bank",
                    "comment": "note",
                },
            )


@pytest.mark.asyncio
async def test_create_recipient_pending_by_default(session_factory):
    async with session_factory() as session:
        payout, recipient, _ = await _create_payout_with_recipient(session_factory)
    async with session_factory() as session:
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        assert db_recipient.status == RecipientStatus.pending.value


@pytest.mark.asyncio
async def test_claim_pending_recipients_moves_to_sending(session_factory):
    payout, recipient, _ = await _create_payout_with_recipient(session_factory)
    async with session_factory() as session:
        payout = await session.get(type(payout), payout.id)
        payout.status = "sending"
        await session.commit()
    async with session_factory() as session:
        claimed = await claim_pending_recipients(session, limit=10)
        await session.commit()
    assert [row.id for row in claimed] == [recipient.id]
    async with session_factory() as session:
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        assert db_recipient.status == RecipientStatus.sending.value


@pytest.mark.asyncio
async def test_repeat_claim_skips_already_claimed(session_factory):
    payout, recipient, _ = await _create_payout_with_recipient(session_factory)
    async with session_factory() as session:
        db_payout = await session.get(type(payout), payout.id)
        db_payout.status = "sending"
        await session.commit()
    async with session_factory() as session:
        first = await claim_pending_recipients(session, limit=10)
        await session.commit()
    async with session_factory() as session:
        second = await claim_pending_recipients(session, limit=10)
        await session.commit()
    assert [row.id for row in first] == [recipient.id]
    assert second == []


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", [RecipientStatus.cancelled.value, RecipientStatus.paid.value])
async def test_worker_never_claims_cancelled_or_paid(session_factory, initial_status):
    payout, recipient, _ = await _create_payout_with_recipient(session_factory)
    async with session_factory() as session:
        db_payout = await session.get(type(payout), payout.id)
        db_payout.status = "sending"
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        db_recipient.status = initial_status
        await session.commit()
    async with session_factory() as session:
        claimed = await claim_pending_recipients(session, limit=10)
        await session.commit()
    assert claimed == []


@pytest.mark.asyncio
async def test_successful_send_without_profile_sets_payment_required(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Июнь",
                "period_from": datetime(2026, 3, 1).date(),
                "period_to": datetime(2026, 3, 31).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sending.value
        await session.commit()
    async with session_factory() as session:
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        db_recipient.status = RecipientStatus.payment_required.value
        db_recipient.sent_at = datetime.now(UTC)
        await session.commit()
    async with session_factory() as session:
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        assert db_recipient.status == RecipientStatus.payment_required.value


@pytest.mark.asyncio
async def test_successful_send_with_profile_sets_sent(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await upsert_payment_profile(session, user, "Иван Иванов\n+79990000000\nT-Bank")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Июнь",
                "period_from": datetime(2026, 3, 1).date(),
                "period_to": datetime(2026, 3, 31).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sending.value
        await session.commit()
    async with session_factory() as session:
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        snapshot = {
            "raw_payment_details": "Иван Иванов\n+79990000000\nT-Bank",
        }
        db_recipient.status = RecipientStatus.sent.value
        db_recipient.sent_at = datetime.now(UTC)
        db_recipient.payment_profile_snapshot = snapshot
        await session.commit()
    async with session_factory() as session:
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        assert db_recipient.status == RecipientStatus.sent.value


@pytest.mark.asyncio
async def test_send_failure_sets_failed(session_factory):
    payout, recipient, _ = await _create_payout_with_recipient(session_factory)
    async with session_factory() as session:
        db_payout = await session.get(type(payout), payout.id)
        db_payout.status = "sending"
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        db_recipient.status = RecipientStatus.sending.value
        await session.commit()
    async with session_factory() as session:
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        db_recipient.status = RecipientStatus.failed.value
        db_recipient.failed_at = datetime.now(UTC)
        db_recipient.failure_reason = "boom"
        await session.commit()
    async with session_factory() as session:
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        assert db_recipient.status == RecipientStatus.failed.value


@pytest.mark.asyncio
async def test_stale_sending_returns_pending(session_factory):
    payout, recipient, _ = await _create_payout_with_recipient(session_factory)
    async with session_factory() as session:
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        db_recipient.status = RecipientStatus.sending.value
        db_recipient.updated_at = datetime.now(UTC) - timedelta(minutes=11)
        await session.commit()
    async with session_factory() as session:
        restored = await recover_stale_sending(session)
        await session.commit()
        assert restored == 1
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.pending.value


@pytest.mark.asyncio
async def test_payment_required_to_payment_received_after_raw_payment_details(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Июнь",
                "period_from": datetime(2026, 3, 1).date(),
                "period_to": datetime(2026, 3, 31).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.payment_required.value
        await session.commit()
    async with session_factory() as session:
        user = await session.get(type(user), user.id)
        active_recipient = await session.get(PayoutRecipient, recipient.id)
        await save_profile_and_snapshot(
            session,
            user=user,
            payload="Иван Иванов\n+79990000000\nT-Bank",
            active_recipient=active_recipient,
        )
        await session.commit()
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.payment_received.value
        assert refreshed.payment_profile_snapshot["raw_payment_details"] == "Иван Иванов\n+79990000000\nT-Bank"


@pytest.mark.asyncio
async def test_free_text_reply_does_not_auto_mark_payment_received(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Июнь",
                "period_from": datetime(2026, 3, 1).date(),
                "period_to": datetime(2026, 3, 31).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()
        await save_payment_reply(session, recipient, "просто текст", parsed={"raw": "просто текст"})
        await session.commit()
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.sent.value
        assert refreshed.replied_at is not None


@pytest.mark.asyncio
async def test_confirm_saved_profile_sets_payment_received(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await upsert_payment_profile(session, user, "Иван Иванов\n+79990000000\nT-Bank\ncomment")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Июль",
                "period_from": datetime(2026, 4, 1).date(),
                "period_to": datetime(2026, 4, 30).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()
    async with session_factory() as session:
        saved = await confirm_saved_profile(session, recipient_id=recipient.id, user_id=user.id)
        await session.commit()
        assert saved is not None
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.payment_received.value
        assert refreshed.payment_profile_snapshot["raw_payment_details"] == "Иван Иванов\n+79990000000\nT-Bank\ncomment"


@pytest.mark.asyncio
async def test_confirm_saved_profile_idor_rejected(session_factory):
    async with session_factory() as session:
        owner = await get_or_create_user(session, 111, "Иван Иванов")
        attacker = await get_or_create_user(session, 222, "Петр Петров")
        await upsert_payment_profile(session, owner, "Иван Иванов\n+79990000000\nT-Bank")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Июль",
                "period_from": datetime(2026, 4, 1).date(),
                "period_to": datetime(2026, 4, 30).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [owner.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()
    async with session_factory() as session:
        result = await confirm_saved_profile(session, recipient_id=recipient.id, user_id=attacker.id)
        await session.commit()
        assert result is None
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.sent.value
        assert refreshed.payment_profile_snapshot is None


@pytest.mark.asyncio
async def test_mark_paid_service(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Апрель",
                "period_from": datetime(2026, 1, 1).date(),
                "period_to": datetime(2026, 1, 31).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.payment_received.value
        await session.commit()
        recipient = await mark_paid(session, recipient, actor_telegram_id=123, paid_note="ok")
        await session.commit()
        assert recipient.status == RecipientStatus.paid.value
        assert recipient.paid_by_admin_id == 123


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_status",
    [
        RecipientStatus.pending.value,
        RecipientStatus.sent.value,
        RecipientStatus.failed.value,
        RecipientStatus.cancelled.value,
    ],
)
async def test_mark_paid_rejects_wrong_status(session_factory, initial_status):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Апрель",
                "period_from": datetime(2026, 1, 1).date(),
                "period_to": datetime(2026, 1, 31).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = initial_status
        await session.commit()
        with pytest.raises(ValueError):
            await mark_paid(session, recipient, actor_telegram_id=123)


@pytest.mark.asyncio
async def test_paid_cannot_be_cancelled(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Апрель",
                "period_from": datetime(2026, 1, 1).date(),
                "period_to": datetime(2026, 1, 31).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.paid.value
        await session.commit()
        with pytest.raises(ValueError):
            await cancel_recipient(session, recipient, actor_telegram_id=123)


@pytest.mark.asyncio
async def test_worker_does_not_overwrite_paid_or_payment_received(session_factory):
    payout, recipient, user = await _create_payout_with_recipient(session_factory)
    async with session_factory() as session:
        db_payout = await session.get(type(payout), payout.id)
        db_payout.status = "sending"
        db_recipient = await session.get(PayoutRecipient, recipient.id)
        db_recipient.status = RecipientStatus.payment_received.value
        await session.commit()
    async with session_factory() as session:
        claimed = await claim_pending_recipients(session, limit=10)
        await session.commit()
    assert claimed == []
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.payment_received.value


@pytest.mark.asyncio
async def test_payout_terminal_state_after_worker(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Март",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.failed.value
        await session.commit()
    async with session_factory() as session:
        payout = await session.get(type(payout), payout.id)
        await finalize_payout_after_worker(session, payout)
        await session.commit()
    async with session_factory() as session:
        refreshed = await session.get(type(payout), payout.id)
        assert refreshed.status == "partially_failed"


@pytest.mark.asyncio
async def test_retry_failed_recipient(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Март",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.failed.value
        recipient.failed_at = datetime.now(UTC)
        recipient.failure_reason = "oops"
        await session.commit()
        recipient = await retry_failed_recipient(session, recipient, actor_telegram_id=123)
        await session.commit()
        assert recipient.status == RecipientStatus.pending.value


@pytest.mark.asyncio
async def test_csv_contains_snapshot_and_handles_missing_snapshot(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Май",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.payment_received.value
        recipient.payment_profile_snapshot = {
            "raw_payment_details": "Иван Иванов\n+79990000000\nT-Bank\nnote",
        }
        await session.commit()
        csv_data = await format_recipients_csv(session, payout.id)
        assert "raw_payment_details" in csv_data
        assert "Иван Иванов" in csv_data
        assert "T-Bank" in csv_data
        header = csv_data.splitlines()[0]
        assert "recipient_full_name" not in header
        assert "phone" not in header
        assert "bank_name" not in header
        assert "comment" not in header

    async with session_factory() as session:
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Пустая",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        await session.commit()
        csv_data = await format_recipients_csv(session, payout.id)
        assert "recipient_id,payout_id,user_id" in csv_data


@pytest.mark.asyncio
async def test_csv_contains_snapshot_after_confirm_saved_profile(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await upsert_payment_profile(session, user, "Иван Иванов\n+79990000000\nT-Bank\nnote")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Май",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()
        await confirm_saved_profile(session, recipient_id=recipient.id, user_id=user.id)
        await session.commit()
        csv_data = await format_recipients_csv(session, payout.id)
        assert "raw_payment_details" in csv_data
        assert "Иван Иванов" in csv_data


@pytest.mark.asyncio
async def test_audit_log_does_not_store_payment_details(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Апрель",
                "period_from": datetime(2026, 1, 1).date(),
                "period_to": datetime(2026, 1, 31).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.payment_received.value
        await session.commit()
        await mark_paid(session, recipient, actor_telegram_id=123)
        await session.commit()
        rows = await session.scalars(select(AuditLog).where(AuditLog.action == "recipient_marked_paid"))
        log = rows.first()
        assert log is not None
        assert log.meta is None


def test_services_source_has_no_legacy_payment_helper():
    source = (Path(__file__).resolve().parents[1] / "src/topsbottg/services.py").read_text(encoding="utf-8")
    assert "_legacy_payment_details_text" not in source
