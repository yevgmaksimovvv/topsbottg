from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.config import Settings


def make_engine(settings: Settings):
    connect_args = {}
    pool_kwargs = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    else:
        pool_kwargs = {
            "pool_size": min(5, settings.postgresql_max_connections),
            "max_overflow": 0,
        }
    return create_async_engine(
        settings.database_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
        **pool_kwargs,
    )


def make_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session
