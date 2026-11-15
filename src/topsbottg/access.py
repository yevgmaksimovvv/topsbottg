"""Server-side authorization: environment owners and revocable database admins."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from topsbottg.models import AdminGrant, AuditLog

AdminRole = Literal["owner", "admin"]


@dataclass(frozen=True, slots=True)
class Principal:
    telegram_id: int
    role: AdminRole

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


class AccessDenied(PermissionError):
    """The authenticated Telegram identity lacks the required privileges."""


async def get_principal(
    session: AsyncSession, telegram_id: int, owner_ids: set[int]
) -> Principal | None:
    """Owners require no User row or database lookup; dynamic grants fail closed."""
    if telegram_id in owner_ids:
        return Principal(telegram_id, "owner")
    active = await session.scalar(
        select(AdminGrant.id).where(
            AdminGrant.telegram_id == telegram_id,
            AdminGrant.revoked_at.is_(None),
        )
    )
    return Principal(telegram_id, "admin") if active is not None else None


async def require_admin(
    session: AsyncSession, telegram_id: int, owner_ids: set[int]
) -> Principal:
    principal = await get_principal(session, telegram_id, owner_ids)
    if principal is None:
        raise AccessDenied("admin only")
    return principal


def require_owner(telegram_id: int, owner_ids: set[int]) -> Principal:
    if telegram_id not in owner_ids:
        raise AccessDenied("owner only")
    return Principal(telegram_id, "owner")


async def grant_admin(
    session: AsyncSession, *, actor_telegram_id: int, target_telegram_id: int, owner_ids: set[int]
) -> bool:
    """Internal grant operation for the invitation workflow (PR 05), not a public REST endpoint."""
    require_owner(actor_telegram_id, owner_ids)
    if target_telegram_id <= 0 or target_telegram_id in owner_ids:
        raise ValueError("Cannot grant an owner or invalid Telegram ID.")
    existing = await session.scalar(
        select(AdminGrant).where(AdminGrant.telegram_id == target_telegram_id)
    )
    if existing is not None and existing.revoked_at is None:
        return False
    now = datetime.now(UTC)
    values = {
        "telegram_id": target_telegram_id,
        "granted_by_telegram_id": actor_telegram_id,
        "granted_at": now,
        "revoked_at": None,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        stmt = pg_insert(AdminGrant).values(**values)
    elif dialect == "sqlite":
        stmt = sqlite_insert(AdminGrant).values(**values)
    else:
        raise RuntimeError(f"Unsupported dialect for admin grants: {dialect}")
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[AdminGrant.telegram_id],
            set_={
                "granted_by_telegram_id": actor_telegram_id,
                "granted_at": now,
                "revoked_at": None,
                "updated_at": now,
            },
        )
    )
    session.add(
        AuditLog(
            actor_telegram_id=actor_telegram_id,
            action="admin_granted",
            entity_type="admin_grant",
            entity_id=str(target_telegram_id),
        )
    )
    await session.flush()
    return True


async def revoke_admin(
    session: AsyncSession, *, actor_telegram_id: int, target_telegram_id: int, owner_ids: set[int]
) -> bool:
    require_owner(actor_telegram_id, owner_ids)
    if target_telegram_id in owner_ids:
        raise ValueError("An environment owner cannot be revoked.")
    now = datetime.now(UTC)
    result = await session.execute(
        update(AdminGrant)
        .where(
            AdminGrant.telegram_id == target_telegram_id,
            AdminGrant.revoked_at.is_(None),
        )
        .values(revoked_at=now, updated_at=now)
    )
    if not result.rowcount:
        return False
    session.add(
        AuditLog(
            actor_telegram_id=actor_telegram_id,
            action="admin_revoked",
            entity_type="admin_grant",
            entity_id=str(target_telegram_id),
        )
    )
    await session.flush()
    return True
