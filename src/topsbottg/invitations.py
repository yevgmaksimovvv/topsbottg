from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from topsbottg.access import AccessDenied, get_principal, grant_admin_from_invitation, require_owner
from topsbottg.models import AdminInvitation, AuditLog, User

INVITATION_TTL = timedelta(hours=48)


class InvitationConflict(ValueError):
    pass


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _audit(session: AsyncSession, actor_id: int, action: str, invitation_id: int) -> None:
    session.add(
        AuditLog(
            actor_telegram_id=actor_id,
            action=action,
            entity_type="admin_invitation",
            entity_id=str(invitation_id),
        )
    )


async def find_candidate(session: AsyncSession, *, telegram_id: int) -> User | None:
    """An invitation target must already exist in the registration database."""
    if telegram_id <= 0:
        raise ValueError("Telegram ID must be positive.")
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def search_candidates(
    session: AsyncSession, *, name: str, study_group_id: int | None = None,
    limit: int = 30, offset: int = 0,
) -> tuple[list[User], bool]:
    """Find registered users by any fragments of their name, independently of group membership."""
    words = name.split()
    if not words:
        return [], False
    query = (
        select(User)
        .options(selectinload(User.study_group))
        .where(User.full_name != "Без ФИО")
    )
    for word in words:
        # LIKE wildcards in names are literal, not a way to enumerate all users.
        literal = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(User.full_name.ilike(f"%{literal}%", escape="\\"))
    if study_group_id is not None:
        query = query.where(User.study_group_id == study_group_id)
    rows = list((await session.scalars(
        query.order_by(func.lower(User.full_name), User.id).offset(offset).limit(limit + 1)
    )).all())
    return rows[:limit], len(rows) > limit


async def create_invitation(
    session: AsyncSession, *, actor_id: int, target_id: int, owner_ids: set[int],
) -> AdminInvitation:
    require_owner(actor_id, owner_ids)
    if target_id <= 0 or target_id in owner_ids:
        raise InvitationConflict("Нельзя пригласить владельца или неверный Telegram ID.")
    now = datetime.now(UTC)
    # Блокировка строки пользователя не даёт двум владельцам создать дублирующие приглашения.
    target = await session.scalar(
        select(User).where(User.telegram_id == target_id).with_for_update()
    )
    if target is None:
        raise InvitationConflict("Пользователь не найден. Попросите его отправить /start.")
    if await get_principal(session, target_id, owner_ids) is not None:
        raise InvitationConflict("У пользователя уже есть административный доступ.")

    await session.execute(
        update(AdminInvitation)
        .where(
            AdminInvitation.target_telegram_id == target_id,
            AdminInvitation.status == "pending",
            AdminInvitation.expires_at <= now,
        )
        .values(status="expired", decided_at=now)
    )
    existing = await session.scalar(
        select(AdminInvitation.id).where(
            AdminInvitation.target_telegram_id == target_id, AdminInvitation.status == "pending"
        )
    )
    if existing is not None:
        raise InvitationConflict("Приглашение этому пользователю уже отправлено.")

    invitation = AdminInvitation(
        target_telegram_id=target_id,
        created_by_owner_id=actor_id,
        expires_at=now + INVITATION_TTL,
        status="pending",
    )
    session.add(invitation)
    await session.flush()
    _audit(session, actor_id, "admin_invitation_created", invitation.id)
    return invitation


async def cancel_invitation(
    session: AsyncSession, *, actor_id: int, invitation_id: int, owner_ids: set[int]
) -> str:
    require_owner(actor_id, owner_ids)
    invitation = await session.scalar(
        select(AdminInvitation).where(AdminInvitation.id == invitation_id).with_for_update()
    )
    if invitation is None:
        return "missing"
    if invitation.status != "pending":
        return invitation.status
    now = datetime.now(UTC)
    invitation.status = "expired" if _utc(invitation.expires_at) <= now else "cancelled"
    invitation.decided_at = now
    _audit(session, actor_id, "admin_invitation_" + invitation.status, invitation.id)
    await session.flush()
    return invitation.status


async def decide_invitation(
    session: AsyncSession, *, invitation_id: int, actor_id: int, accept: bool, owner_ids: set[int]
) -> str:
    invitation = await session.scalar(
        select(AdminInvitation).where(AdminInvitation.id == invitation_id).with_for_update()
    )
    if invitation is None:
        return "missing"
    if invitation.target_telegram_id != actor_id:
        raise AccessDenied("This invitation belongs to another Telegram account.")
    if invitation.status != "pending":
        return invitation.status
    now = datetime.now(UTC)
    if _utc(invitation.expires_at) <= now:
        invitation.status = "expired"
    elif actor_id in owner_ids:
        invitation.status = "cancelled"
    elif accept:
        await grant_admin_from_invitation(
            session, invitation=invitation, target_telegram_id=actor_id,
            owner_ids=owner_ids,
        )
        invitation.status = "accepted"
    else:
        invitation.status = "declined"
    invitation.decided_at = now
    _audit(session, actor_id, "admin_invitation_" + invitation.status, invitation.id)
    await session.flush()
    return invitation.status


async def list_invitations(session: AsyncSession, *, actor_id: int, owner_ids: set[int]) -> list[AdminInvitation]:
    require_owner(actor_id, owner_ids)
    return list(
        (await session.scalars(
            select(AdminInvitation).order_by(AdminInvitation.id.desc()).limit(100)
        )).all()
    )
