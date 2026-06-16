from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.api import create_app
from topsbottg.config import Settings
from topsbottg.models import Base


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "tests.db"
    return Settings(
        app_domain="localhost",
        bot_token="test-token",
        database_url=f"sqlite+aiosqlite:///{db_path}",
        admin_telegram_ids="123",
        mini_app_url="https://localhost/miniapp/",
        broadcast_rate_per_second=5,
        environment="test",
    )


@pytest.fixture
async def engine(settings: Settings):
    engine = create_async_engine(settings.database_url, future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
def app(settings: Settings, session_factory):
    return create_app(settings, session_factory)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def make_init_data(bot_token: str, user_id: int, *, auth_date: int | None = None) -> str:
    if auth_date is None:
        auth_date = int(datetime.now(UTC).timestamp())
    payload = {
        "auth_date": str(auth_date),
        "query_id": "AAE",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    payload["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(payload, quote_via=quote)


@pytest.fixture
def admin_init_data(settings: Settings) -> str:
    return make_init_data(settings.bot_token, 123)


@pytest.fixture
def non_admin_init_data(settings: Settings) -> str:
    return make_init_data(settings.bot_token, 999)


@pytest.fixture
def invalid_init_data(settings: Settings) -> str:
    value = make_init_data(settings.bot_token, 999)
    return value.replace("hash=", "hash=0", 1)


@pytest.fixture
def expired_init_data(settings: Settings) -> str:
    auth_date = int((datetime.now(UTC) - timedelta(days=2)).timestamp())
    return make_init_data(settings.bot_token, 999, auth_date=auth_date)


@pytest.fixture
def missing_auth_date_init_data(settings: Settings) -> str:
    payload = {
        "query_id": "AAE",
        "user": json.dumps({"id": 999, "first_name": "Test"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    payload["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(payload, quote_via=quote)
