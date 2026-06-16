from __future__ import annotations

import asyncio
import json
import urllib.request

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from topsbottg.config import Settings
from topsbottg.db import get_session
from topsbottg.models import PaymentProfile, Payout, PayoutRecipient, User
from topsbottg.schemas import (
    AddRecipientsIn,
    MarkPaidIn,
    PaymentProfileRevealOut,
    PayoutCreate,
    PayoutOut,
    PayoutUpdate,
    RecipientOut,
    ReplyOut,
    UserOut,
    UserPageOut,
)
from topsbottg.security import InitDataError, validate_telegram_init_data
from topsbottg.services import (
    add_recipients,
    cancel_payout,
    cancel_recipient,
    complete_payout,
    create_payout,
    format_recipients_csv,
    get_payment_profile_for_admin_reveal,
    get_payout,
    get_payout_recipients,
    get_user_by_id,
    list_payouts,
    list_users,
    log_audit,
    mark_paid,
    retry_failed_recipient,
    set_payout_sending,
    update_payout,
)


def _require_admin(init_data: str | None, settings: Settings) -> int:
    if not init_data:
        raise HTTPException(status_code=401, detail="initData required")
    try:
        data = validate_telegram_init_data(
            init_data,
            settings.bot_token,
            max_age_seconds=settings.mini_app_init_data_max_age_seconds,
        )
    except InitDataError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if data.user_id not in settings.admin_ids_set:
        raise HTTPException(status_code=403, detail="admin only")
    return data.user_id


def serialize_user(user: User, profile: PaymentProfile | None) -> UserOut:
    has_payment_profile = bool(profile and profile.raw_payment_details and profile.raw_payment_details.strip())
    return UserOut(
        id=user.id,
        telegram_user_id=user.telegram_id,
        telegram_id=user.telegram_id,
        full_name=user.full_name,
        is_active=user.is_active,
        has_payment_profile=has_payment_profile,
        payment_profile_id=getattr(profile, "id", None) if has_payment_profile else None,
    )


def serialize_payment_reveal(user: User, profile: PaymentProfile) -> PaymentProfileRevealOut:
    return PaymentProfileRevealOut(
        user_id=user.id,
        telegram_user_id=user.telegram_id,
        full_name=user.full_name,
        raw_payment_details=profile.raw_payment_details,
    )


def serialize_payout(payout: Payout) -> PayoutOut:
    return PayoutOut(
        id=payout.id,
        title=payout.title,
        period_from=payout.period_from,
        period_to=payout.period_to,
        message_template=payout.message_template,
        status=payout.status,
        created_by_telegram_id=payout.created_by_telegram_id,
        created_at=payout.created_at,
        updated_at=payout.updated_at,
    )


def serialize_recipient(recipient: PayoutRecipient) -> RecipientOut:
    reply = None
    if recipient.payment_replies:
        last_reply = recipient.payment_replies[-1]
        reply = ReplyOut(
            id=last_reply.id,
            raw_text=last_reply.raw_text,
            parsed=last_reply.parsed,
            created_at=last_reply.created_at,
        )
    return RecipientOut(
        id=recipient.id,
        user_id=recipient.user_id,
        full_name=recipient.user.full_name,
        telegram_user_id=recipient.user.telegram_id,
        telegram_id=recipient.user.telegram_id,
        status=recipient.status,
        sent_at=recipient.sent_at,
        failed_at=recipient.failed_at,
        failure_reason=recipient.failure_reason,
        replied_at=recipient.replied_at,
        paid_at=recipient.paid_at,
        paid_by_admin_id=recipient.paid_by_admin_id,
        paid_note=recipient.paid_note,
        payment_profile_snapshot=recipient.payment_profile_snapshot,
        reply=reply,
    )


async def check_database_ready(session_factory) -> bool:
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


def _check_telegram_ready_sync(bot_token: str, timeout_seconds: float) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            if getattr(response, "status", None) != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("ok") is True


async def check_telegram_ready(bot_token: str, timeout_seconds: float = 3.0) -> bool:
    return await asyncio.to_thread(_check_telegram_ready_sync, bot_token, timeout_seconds)


async def get_readiness_checks(settings: Settings, session_factory) -> dict[str, str]:
    database_ok = await check_database_ready(session_factory)
    telegram_ok = await check_telegram_ready(settings.bot_token)
    return {
        "database": "ok" if database_ok else "failed",
        "telegram": "ok" if telegram_ok else "failed",
    }


def create_app(settings: Settings, session_factory) -> FastAPI:
    app = FastAPI(title="topsbottg")
    app.state.settings = settings
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def admin_cache_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/admin/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        checks = await get_readiness_checks(settings, session_factory)
        status = "ok" if all(value == "ok" for value in checks.values()) else "error"
        return JSONResponse({"status": status, "checks": checks}, status_code=200 if status == "ok" else 503)

    @app.get("/api/admin/me")
    async def admin_me(
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> dict[str, object]:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        return {"telegram_user_id": telegram_id, "telegram_id": telegram_id, "is_admin": True}

    @app.get("/api/admin/users")
    async def admin_users(
        search: str | None = Query(default=None),
        has_payment_profile: bool | None = Query(default=None),
        is_active: bool | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> UserPageOut:
        _require_admin(x_telegram_init_data, settings)
        rows = await list_users(
            session,
            search=search,
            has_payment_profile=has_payment_profile,
            is_active=is_active,
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(rows) > limit
        if not has_more and len(rows) == limit and limit == 100:
            extra_rows = await list_users(
                session,
                search=search,
                has_payment_profile=has_payment_profile,
                is_active=is_active,
                limit=1,
                offset=offset + limit,
            )
            has_more = bool(extra_rows)
        page_rows = rows[:limit]
        return UserPageOut(
            items=[serialize_user(user, profile) for user, profile in page_rows],
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

    @app.get("/api/admin/users/{user_id}")
    async def admin_user_detail(
        user_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> dict[str, object]:
        _require_admin(x_telegram_init_data, settings)
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        profile = await get_payment_profile_for_admin_reveal(session, user.id)
        return {
            "user": serialize_user(user, profile),
            "payment_profile": (
                {"id": profile.id, "user_id": profile.user_id, "deleted_at": profile.deleted_at} if profile else None
            ),
        }

    @app.get("/api/admin/users/{user_id}/payment-details")
    async def admin_user_payment_details(
        user_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> PaymentProfileRevealOut:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        user = await get_user_by_id(session, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        profile = await get_payment_profile_for_admin_reveal(session, user.id)
        if profile is None:
            raise HTTPException(status_code=404, detail="payment details not found")
        await log_audit(
            session,
            actor_telegram_id=telegram_id,
            action="view_payment_details",
            entity_type="user",
            entity_id=str(user.id),
        )
        await session.commit()
        return serialize_payment_reveal(user, profile)

    @app.get("/api/admin/payouts")
    async def admin_payouts(
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> list[PayoutOut]:
        _require_admin(x_telegram_init_data, settings)
        return [serialize_payout(payout) for payout in await list_payouts(session)]

    @app.post("/api/admin/payouts")
    async def admin_create_payout(
        payload: PayoutCreate,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> PayoutOut:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        try:
            payout = await create_payout(session, actor_telegram_id=telegram_id, payload=payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        await session.refresh(payout)
        return serialize_payout(payout)

    @app.get("/api/admin/payouts/{payout_id}")
    async def admin_payout_detail(
        payout_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> dict[str, object]:
        _require_admin(x_telegram_init_data, settings)
        payout = await get_payout(session, payout_id)
        if payout is None:
            raise HTTPException(status_code=404, detail="payout not found")
        return {"payout": serialize_payout(payout)}

    @app.patch("/api/admin/payouts/{payout_id}")
    async def admin_update_payout(
        payout_id: int,
        payload: PayoutUpdate,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> PayoutOut:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        payout = await get_payout(session, payout_id)
        if payout is None:
            raise HTTPException(status_code=404, detail="payout not found")
        try:
            payout = await update_payout(session, payout, actor_telegram_id=telegram_id, payload=payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        await session.refresh(payout)
        return serialize_payout(payout)

    @app.post("/api/admin/payouts/{payout_id}/recipients")
    async def admin_add_recipients(
        payout_id: int,
        payload: AddRecipientsIn,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> dict[str, int]:
        _require_admin(x_telegram_init_data, settings)
        payout = await get_payout(session, payout_id)
        if payout is None:
            raise HTTPException(status_code=404, detail="payout not found")
        created = await add_recipients(session, payout, payload.user_ids)
        await session.commit()
        return {"created": len(created)}

    @app.delete("/api/admin/payouts/{payout_id}/recipients/{recipient_id}")
    async def admin_delete_recipient(
        payout_id: int,
        recipient_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> dict[str, str]:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        payout = await get_payout(session, payout_id)
        if payout is None:
            raise HTTPException(status_code=404, detail="payout not found")
        recipient = await session.scalar(
            select(PayoutRecipient).where(PayoutRecipient.id == recipient_id, PayoutRecipient.payout_id == payout_id)
        )
        if recipient is None:
            raise HTTPException(status_code=404, detail="recipient not found")
        try:
            await cancel_recipient(session, recipient, actor_telegram_id=telegram_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        return {"status": "ok"}

    @app.post("/api/admin/payouts/{payout_id}/send")
    async def admin_send_payout(
        payout_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> PayoutOut:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        payout = await get_payout(session, payout_id)
        if payout is None:
            raise HTTPException(status_code=404, detail="payout not found")
        try:
            payout = await set_payout_sending(session, payout, actor_telegram_id=telegram_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        await session.refresh(payout)
        return serialize_payout(payout)

    @app.post("/api/admin/payouts/{payout_id}/close")
    async def admin_close_payout(
        payout_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> PayoutOut:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        payout = await get_payout(session, payout_id)
        if payout is None:
            raise HTTPException(status_code=404, detail="payout not found")
        try:
            payout = await complete_payout(session, payout, actor_telegram_id=telegram_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        await session.refresh(payout)
        return serialize_payout(payout)

    @app.post("/api/admin/payouts/{payout_id}/cancel")
    async def admin_cancel_payout(
        payout_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> PayoutOut:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        payout = await get_payout(session, payout_id)
        if payout is None:
            raise HTTPException(status_code=404, detail="payout not found")
        try:
            payout = await cancel_payout(session, payout, actor_telegram_id=telegram_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        await session.refresh(payout)
        return serialize_payout(payout)

    @app.post("/api/admin/payouts/{payout_id}/recipients/{recipient_id}/retry")
    async def admin_retry_recipient(
        payout_id: int,
        recipient_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> RecipientOut:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        recipient = await session.scalar(
            select(PayoutRecipient)
            .options(selectinload(PayoutRecipient.user))
            .options(selectinload(PayoutRecipient.payment_replies))
            .where(PayoutRecipient.id == recipient_id, PayoutRecipient.payout_id == payout_id)
        )
        if recipient is None:
            raise HTTPException(status_code=404, detail="recipient not found")
        try:
            recipient = await retry_failed_recipient(session, recipient, actor_telegram_id=telegram_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        return serialize_recipient(recipient)

    @app.get("/api/admin/payouts/{payout_id}/recipients")
    async def admin_payout_recipients(
        payout_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> list[RecipientOut]:
        _require_admin(x_telegram_init_data, settings)
        recipients = await get_payout_recipients(session, payout_id)
        return [serialize_recipient(recipient) for recipient in recipients]

    @app.post("/api/admin/payouts/{payout_id}/recipients/{recipient_id}/mark-paid")
    async def admin_mark_paid(
        payout_id: int,
        recipient_id: int,
        payload: MarkPaidIn,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> RecipientOut:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        recipient = await session.scalar(
            select(PayoutRecipient)
            .options(selectinload(PayoutRecipient.user))
            .options(selectinload(PayoutRecipient.payment_replies))
            .where(PayoutRecipient.id == recipient_id, PayoutRecipient.payout_id == payout_id)
        )
        if recipient is None:
            raise HTTPException(status_code=404, detail="recipient not found")
        try:
            recipient = await mark_paid(
                session,
                recipient,
                actor_telegram_id=telegram_id,
                paid_at=payload.paid_at,
                paid_note=payload.paid_note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        return serialize_recipient(recipient)

    @app.get("/api/admin/payouts/{payout_id}/export.csv")
    async def admin_export_csv(
        payout_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> StreamingResponse:
        telegram_id = _require_admin(x_telegram_init_data, settings)
        payout = await get_payout(session, payout_id)
        if payout is None:
            raise HTTPException(status_code=404, detail="payout not found")
        await log_audit(
            session,
            actor_telegram_id=telegram_id,
            action="export_csv",
            entity_type="payout",
            entity_id=str(payout.id),
        )
        csv_data = await format_recipients_csv(session, payout.id)
        await session.commit()
        return StreamingResponse(
            iter([csv_data]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="payout-{payout.id}.csv"',
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app
