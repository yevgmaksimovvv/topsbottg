"""MTProto username resolution with a bot session; no real Telegram network calls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon import functions, types
from telethon.errors import UsernameNotOccupiedError
from telethon.sessions import MemorySession

import topsbottg.username_resolver as module


class FakeClient:
    def __init__(self, response=None, failure=None):
        self.response = response
        self.failure = failure
        self.connected = False
        self.calls = []
        self.starts = 0
        self.closed = 0
        self.session = None

    def is_connected(self):
        return self.connected

    async def start(self, *, bot_token):
        assert bot_token == "bot-token"
        self.connected = True
        self.starts += 1

    async def __call__(self, request):
        assert isinstance(request, functions.contacts.ResolveUsernameRequest)
        self.calls.append(request.username)
        if self.failure:
            raise self.failure
        return self.response

    async def disconnect(self):
        self.connected = False
        self.closed += 1


def with_fake_telethon(monkeypatch, fake):
    def factory(session, api_id, api_hash, **kwargs):
        assert isinstance(session, MemorySession)
        assert (api_id, api_hash) == (12345, "secret-hash")
        assert kwargs["receive_updates"] is False
        assert kwargs["flood_sleep_threshold"] == 0
        fake.session = session
        return fake
    monkeypatch.setattr(module, "TelegramClient", factory)


@pytest.mark.asyncio
async def test_resolve_user_id_from_raw_mtproto_response(monkeypatch):
    fake = FakeClient(response=SimpleNamespace(
        peer=types.PeerUser(user_id=555),
        users=[types.User(id=555, first_name="Ivan")],
    ))
    with_fake_telethon(monkeypatch, fake)
    resolver = module.MTProtoUsernameResolver(12345, "secret-hash", "bot-token")
    assert await resolver.resolve("@VALID_name") == 555
    assert await resolver.resolve("valid_NAME") == 555
    assert fake.calls == ["valid_name", "valid_name"]
    assert fake.starts == 1
    with pytest.raises(ValueError):
        await resolver.resolve("@no")
    assert fake.calls == ["valid_name", "valid_name"]
    await resolver.close()
    assert fake.closed == 1


@pytest.mark.asyncio
async def test_channel_is_not_a_person(monkeypatch):
    fake = FakeClient(response=SimpleNamespace(
        peer=types.PeerChannel(channel_id=555), users=[],
    ))
    with_fake_telethon(monkeypatch, fake)
    resolver = module.MTProtoUsernameResolver(12345, "secret-hash", "bot-token")
    assert await resolver.resolve("channel_name") is None
    await resolver.close()


@pytest.mark.asyncio
async def test_username_not_occupied_is_not_a_server_failure(monkeypatch):
    fake = FakeClient(failure=UsernameNotOccupiedError(None))
    with_fake_telethon(monkeypatch, fake)
    resolver = module.MTProtoUsernameResolver(12345, "secret-hash", "bot-token")
    assert await resolver.resolve("missing_user") is None
    await resolver.close()


@pytest.mark.asyncio
async def test_network_failure_is_not_reported_as_not_found(monkeypatch):
    fake = FakeClient(failure=OSError("disconnected"))
    with_fake_telethon(monkeypatch, fake)
    resolver = module.MTProtoUsernameResolver(12345, "secret-hash", "bot-token")
    with pytest.raises(module.UsernameLookupUnavailable):
        await resolver.resolve("some_user")
    await resolver.close()
