"""PostgreSQL-backed aiogram FSM state, shared by successive bot processes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StateType, StorageKey
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topsbottg.models import BotFSMState


class DatabaseFSMStorage(BaseStorage):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._key_builder = DefaultKeyBuilder(
            prefix="botfsm", with_bot_id=True,
            with_business_connection_id=True, with_destiny=True,
        )

    def _storage_key(self, key: StorageKey) -> str:
        return self._key_builder.build(key)

    @staticmethod
    def _insert(session: AsyncSession):
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            return pg_insert(BotFSMState)
        if dialect == "sqlite":
            return sqlite_insert(BotFSMState)
        raise NotImplementedError(f"Unsupported FSM storage dialect: {dialect}")

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        async with self._session_factory() as session:
            statement = self._insert(session).values(
                storage_key=self._storage_key(key), state=value, data={}
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[BotFSMState.storage_key],
                    set_={"state": value},
                )
            )
            await session.commit()

    async def set_state_and_data(
        self, key: StorageKey, state: StateType, data: Mapping[str, Any]
    ) -> None:
        value = state.state if isinstance(state, State) else state
        values = dict(data)
        async with self._session_factory() as session:
            statement = self._insert(session).values(
                storage_key=self._storage_key(key), state=value, data=values
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[BotFSMState.storage_key],
                    set_={"state": value, "data": values},
                )
            )
            await session.commit()

    async def has_context(self, key: StorageKey) -> bool:
        """Only contexts touched since the switch from MemoryStorage are safe for inbox capture."""
        async with self._session_factory() as session:
            value = await session.scalar(
                select(BotFSMState.storage_key).where(
                    BotFSMState.storage_key == self._storage_key(key)
                )
            )
            return value is not None

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(BotFSMState.state).where(BotFSMState.storage_key == self._storage_key(key))
            )

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        values = dict(data)
        async with self._session_factory() as session:
            statement = self._insert(session).values(
                storage_key=self._storage_key(key), state=None, data=values
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[BotFSMState.storage_key],
                    set_={"data": values},
                )
            )
            await session.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(BotFSMState.data).where(BotFSMState.storage_key == self._storage_key(key))
            )
            return dict(value or {})

    async def close(self) -> None:
        pass
