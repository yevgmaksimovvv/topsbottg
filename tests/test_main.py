from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import topsbottg.api as api_module
import topsbottg.main as main_module
from topsbottg.config import Settings
from topsbottg.models import Base


def _make_log_record(args) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%s",
        args=args,
        exc_info=None,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "startup.db"
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
async def session_factory(settings: Settings):
    engine = create_async_engine(settings.database_url, future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_healthz_access_log_filter_suppresses_only_successful_healthz() -> None:
    flt = main_module.HealthzAccessLogFilter()

    assert flt.filter(_make_log_record(("127.0.0.1:40944", "GET", "/healthz", "1.1", 200))) is False
    assert flt.filter(_make_log_record(("127.0.0.1:40944", "GET", "/healthz", "1.1", 503))) is True
    assert flt.filter(_make_log_record(("127.0.0.1:40944", "GET", "/api/admin/me", "1.1", 200))) is True
    assert flt.filter(_make_log_record(("127.0.0.1:40944", "POST", "/healthz", "1.1", 200))) is True
    assert flt.filter(_make_log_record(("bad",))) is True


def test_install_access_log_filters_is_idempotent() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    original_filters = list(access_logger.filters)
    try:
        access_logger.filters[:] = [
            flt for flt in access_logger.filters if not isinstance(flt, main_module.HealthzAccessLogFilter)
        ]
        main_module.install_access_log_filters()
        main_module.install_access_log_filters()
        filters = [flt for flt in access_logger.filters if isinstance(flt, main_module.HealthzAccessLogFilter)]
        assert len(filters) == 1
    finally:
        access_logger.filters[:] = original_filters


@pytest.mark.asyncio
async def test_startup_preflight_success_allows_startup_path_to_continue(
    settings: Settings, session_factory, monkeypatch: pytest.MonkeyPatch
):
    async def _database_ok(session_factory_arg) -> bool:  # noqa: ANN001
        return True

    async def _telegram_ok(bot_token: str, timeout_seconds: float = 3.0) -> bool:
        return True

    monkeypatch.setattr(api_module, "check_database_ready", _database_ok)
    monkeypatch.setattr(api_module, "check_telegram_ready", _telegram_ok)

    await main_module.run_startup_preflight(settings, session_factory)


@pytest.mark.asyncio
async def test_startup_preflight_db_failure_exits_non_zero(
    settings: Settings, session_factory, monkeypatch: pytest.MonkeyPatch
):
    async def _database_failed(session_factory_arg) -> bool:  # noqa: ANN001
        return False

    async def _telegram_ok(bot_token: str, timeout_seconds: float = 3.0) -> bool:
        return True

    monkeypatch.setattr(api_module, "check_database_ready", _database_failed)
    monkeypatch.setattr(api_module, "check_telegram_ready", _telegram_ok)

    with pytest.raises(RuntimeError, match="startup readiness failed"):
        await main_module.run_startup_preflight(settings, session_factory)


@pytest.mark.asyncio
async def test_startup_preflight_telegram_failure_exits_non_zero(
    settings: Settings, session_factory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    async def _database_ok(session_factory_arg) -> bool:  # noqa: ANN001
        return True

    async def _telegram_failed(bot_token: str, timeout_seconds: float = 3.0) -> bool:
        return False

    monkeypatch.setattr(api_module, "check_database_ready", _database_ok)
    monkeypatch.setattr(api_module, "check_telegram_ready", _telegram_failed)

    with pytest.raises(RuntimeError, match="startup readiness failed"):
        await main_module.run_startup_preflight(settings, session_factory)
    assert "startup readiness failed: telegram" in caplog.text
