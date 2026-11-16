"""Authenticated text-campaign routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topsbottg.campaigns import (
    campaign_detail,
    campaign_summaries,
    campaign_summary,
    create_campaign,
    decode_campaign_cursor,
    encode_campaign_cursor,
    preview_campaign,
    retry_failed_campaign,
)
from topsbottg.chat_outbox import IdempotencyConflict
from topsbottg.config import Settings
from topsbottg.db import get_session
from topsbottg.models import MessageCampaign
from topsbottg.schemas import (
    CampaignCreateIn,
    CampaignDetailOut,
    CampaignOut,
    CampaignPageOut,
    CampaignPreviewOut,
    CampaignRecipientsIn,
)


def register_campaign_routes(
    app: FastAPI,
    settings: Settings,
    require_admin: Callable[[str | None, Settings, AsyncSession], Awaitable[int]],
) -> None:
    @app.post("/api/admin/message-campaigns/preview", response_model=CampaignPreviewOut)
    async def admin_campaign_preview(
        payload: CampaignRecipientsIn,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> CampaignPreviewOut:
        await require_admin(x_telegram_init_data, settings, session)
        return await preview_campaign(session, payload)

    @app.post("/api/admin/message-campaigns", response_model=CampaignOut, status_code=202)
    async def admin_campaign_create(
        payload: CampaignCreateIn,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> CampaignOut:
        actor_id = await require_admin(x_telegram_init_data, settings, session)
        if (
            idempotency_key is None
            or not 1 <= len(idempotency_key) <= 128
            or not idempotency_key.isascii()
            or any(not 33 <= ord(ch) <= 126 for ch in idempotency_key)
        ):
            raise HTTPException(status_code=422, detail="invalid idempotency key")
        try:
            campaign, _ = await create_campaign(
                session, actor_id=actor_id, key=idempotency_key, payload=payload,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="idempotency key reused with different payload") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await session.commit()
        return await campaign_summary(session, campaign)

    @app.get("/api/admin/message-campaigns", response_model=CampaignPageOut)
    async def admin_campaign_list(
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> CampaignPageOut:
        await require_admin(x_telegram_init_data, settings, session)
        try:
            before = decode_campaign_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid cursor") from exc
        query = select(MessageCampaign).order_by(MessageCampaign.id.desc()).limit(limit + 1)
        if before is not None:
            query = query.where(MessageCampaign.id < before)
        campaigns = (await session.scalars(query)).all()
        selected = campaigns[:limit]
        items = await campaign_summaries(session, selected)
        next_cursor = (
            encode_campaign_cursor(selected[-1].id) if len(campaigns) > limit else None
        )
        return CampaignPageOut(items=items, next_cursor=next_cursor)

    @app.get("/api/admin/message-campaigns/{campaign_id}", response_model=CampaignDetailOut)
    async def admin_campaign_detail(
        campaign_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> CampaignDetailOut:
        await require_admin(x_telegram_init_data, settings, session)
        campaign = await session.get(MessageCampaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        return await campaign_detail(session, campaign)

    @app.post("/api/admin/message-campaigns/{campaign_id}/retry-failed", response_model=CampaignOut)
    async def admin_campaign_retry_failed(
        campaign_id: int,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> CampaignOut:
        await require_admin(x_telegram_init_data, settings, session)
        campaign = await session.get(MessageCampaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        await retry_failed_campaign(session, campaign)
        await session.commit()
        return await campaign_summary(session, campaign)
