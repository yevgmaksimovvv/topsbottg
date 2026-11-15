"""Resolve public Telegram usernames without a local username directory."""

from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient, functions, types
from telethon.errors import RPCError, UsernameInvalidError, UsernameNotOccupiedError
from telethon.sessions import MemorySession

from topsbottg.invitations import normalize_username

logger = logging.getLogger(__name__)


class UsernameLookupUnavailable(RuntimeError):
    """Telegram could not be reached or rejected the MTProto request."""


class MTProtoUsernameResolver:
    """One bot-authorized MTProto connection per app process; no session file."""

    def __init__(self, api_id: int, api_hash: str, bot_token: str) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._bot_token = bot_token
        self._lock = asyncio.Lock()
        self._client: TelegramClient | None = None

    async def resolve(self, value: str) -> int | None:
        username = normalize_username(value)
        async with self._lock:
            try:
                if self._client is None:
                    self._client = TelegramClient(
                        MemorySession(), self._api_id, self._api_hash,
                        receive_updates=False, connection_retries=1,
                        request_retries=0, flood_sleep_threshold=0, timeout=5,
                    )
                if not self._client.is_connected():
                    await asyncio.wait_for(self._client.start(bot_token=self._bot_token), timeout=12)
                result = await asyncio.wait_for(
                    self._client(functions.contacts.ResolveUsernameRequest(username=username)),
                    timeout=10,
                )
            except (UsernameNotOccupiedError, UsernameInvalidError):
                return None
            except (RPCError, OSError, asyncio.TimeoutError) as exc:
                logger.warning("MTProto username lookup failed: %s", type(exc).__name__)
                raise UsernameLookupUnavailable from exc

        # Channels and groups also have usernames, but cannot be made administrators.
        if not isinstance(result.peer, types.PeerUser):
            return None
        user_id = result.peer.user_id
        if not any(isinstance(user, types.User) and user.id == user_id for user in result.users):
            return None
        return user_id

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.disconnect()
                self._client = None
