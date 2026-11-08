from __future__ import annotations

import pytest
from sqlalchemy import select

from topsbottg.models import AuditLog, Payout, PayoutReadyNotification, PayoutRecipient, PayoutStatus, RecipientStatus
from topsbottg.services import (
    add_recipients,
    claim_ready_notifications,
    create_payout,
    delete_payout,
    get_or_create_user,
    get_payout,
    list_payouts,
    queue_payout_ready,
    retry_failed_recipient,
    settle_ready_notification,
)


def payload():
    return {"period_start_day": 1, "period_start_month": 11, "period_end_day": 7, "period_end_month": 11}


@pytest.mark.asyncio
async def test_delete_hides_payout_but_keeps_financial_and_audit_records(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Получатель")
        payout = await create_payout(session, actor_telegram_id=123, payload=payload())
        [recipient] = await add_recipients(session, payout, [user.id])
        payout.status = PayoutStatus.completed.value
        recipient.status = RecipientStatus.paid.value
        await session.commit()
        payout_id, recipient_id = payout.id, recipient.id
    async with session_factory() as session:
        payout = await session.get(Payout, payout_id)
        await delete_payout(session, payout, actor_telegram_id=123)
        await session.commit()
        assert await get_payout(session, payout_id) is None
        assert all(item.id != payout_id for item in await list_payouts(session))
        assert (await session.get(PayoutRecipient, recipient_id)).status == RecipientStatus.paid.value
        assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "delete_payout")) is not None


@pytest.mark.asyncio
async def test_payout_history_cursor_keeps_pages_stable(session_factory):
    async with session_factory() as session:
        payouts = [await create_payout(session, actor_telegram_id=123, payload=payload()) for _ in range(3)]
        await session.commit()
        first_page = await list_payouts(session, limit=2)
        second_page = await list_payouts(session, limit=2, before_id=first_page[-1].id)
        assert [payout.id for payout in first_page] == [payouts[2].id, payouts[1].id]
        assert [payout.id for payout in second_page] == [payouts[0].id]


@pytest.mark.asyncio
async def test_ready_notification_is_persisted_once_and_retried(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Получатель")
        payout = await create_payout(session, actor_telegram_id=123, payload=payload())
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.payment_received.value
        payout.status = PayoutStatus.sent.value
        await session.flush()
        assert await queue_payout_ready(session, payout.id) is True
        assert await queue_payout_ready(session, payout.id) is False
        await session.commit()
    async with session_factory() as session:
        [notice] = await claim_ready_notifications(session)
        assert notice.telegram_id == 123
        assert "подтвердили реквизиты" in notice.message
        await session.commit()
    async with session_factory() as session:
        await settle_ready_notification(session, notice.id)
        await session.commit()
        assert (await session.get(PayoutReadyNotification, notice.id)).delivered_at is not None
        assert await claim_ready_notifications(session) == []


@pytest.mark.asyncio
async def test_retry_reopens_partially_failed_payout(session_factory):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Получатель")
        payout = await create_payout(session, actor_telegram_id=123, payload=payload())
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.failed.value
        payout.status = PayoutStatus.partially_failed.value
        await session.commit()
        await retry_failed_recipient(session, recipient, actor_telegram_id=123)
        await session.commit()
        assert recipient.status == RecipientStatus.pending.value
        assert payout.status == PayoutStatus.sending.value

@pytest.mark.asyncio
async def test_add_recipients_rejected_after_payout_sent(client, session_factory, admin_init_data):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Получатель")
        payout = await create_payout(session, actor_telegram_id=123, payload=payload())
        payout.status = PayoutStatus.sent.value
        await session.commit()
        payout_id, user_id = payout.id, user.id
    response = await client.post(
        f"/api/admin/payouts/{payout_id}/recipients",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={"user_ids": [user_id]},
    )
    assert response.status_code == 409
    async with session_factory() as session:
        assert await session.scalar(select(PayoutRecipient.id).where(PayoutRecipient.payout_id == payout_id)) is None
