from __future__ import annotations

import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from topsbottg.api import create_app, get_readiness_checks
from topsbottg.bot import build_router
from topsbottg.config import Settings, get_settings
from topsbottg.db import make_engine, make_session_factory
from topsbottg.logging_utils import log_event
from topsbottg.worker import run_worker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class HealthzAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, (tuple, list)) or len(args) < 5:
            return True
        _client_addr, method, path, _http_version, status_code = args[:5]
        return not (method == "GET" and path == "/healthz" and status_code == 200)


def install_access_log_filters() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(log_filter, HealthzAccessLogFilter) for log_filter in access_logger.filters):
        access_logger.addFilter(HealthzAccessLogFilter())


async def run_startup_preflight(settings: Settings, session_factory) -> None:
    checks = await get_readiness_checks(settings, session_factory)
    failed_checks = [name for name, status in checks.items() if status != "ok"]
    if failed_checks:
        log_event(
            logger,
            "ERROR",
            "app_startup_failed",
            "Проверка запуска не пройдена",
            reason=",".join(failed_checks),
            db_ready=checks.get("database") == "ok",
            telegram_ready=checks.get("telegram") == "ok",
        )
        logger.error("startup readiness failed: %s", ", ".join(failed_checks))
        raise RuntimeError("startup readiness failed")
    log_event(
        logger,
        "INFO",
        "app_startup_ready",
        "Проверки запуска успешно пройдены",
        db_ready=True,
        telegram_ready=True,
    )


def create_runtime(settings: Settings) -> FastAPI:
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    app = create_app(settings, session_factory)

    @app.on_event("startup")
    async def startup() -> None:
        log_event(
            logger,
            "INFO",
            "app_startup_started",
            "Приложение запускается",
            mini_app_url_present=bool(settings.mini_app_url),
            bot_token_present=bool(settings.bot_token),
            admin_ids_count=len(settings.admin_ids_set),
        )
        await run_startup_preflight(settings, session_factory)
        app.state.stop_event = asyncio.Event()
        bot = Bot(token=settings.bot_token)
        dp = Dispatcher()
        router = build_router(session_factory, settings)
        dp.include_router(router)
        app.state.bot = bot
        app.state.dispatcher = dp
        # Один Python-процесс держит и polling, и worker, чтобы не получить два потребителя одной очереди.
        app.state.bot_task = asyncio.create_task(dp.start_polling(bot))
        app.state.worker_task = asyncio.create_task(run_worker(bot, session_factory, settings, app.state.stop_event))

    @app.on_event("shutdown")
    async def shutdown() -> None:
        app.state.stop_event.set()
        for task_name in ("bot_task", "worker_task"):
            task = getattr(app.state, task_name, None)
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in [getattr(app.state, "bot_task", None), getattr(app.state, "worker_task", None)]
                if task is not None
            ),
            return_exceptions=True,
        )
        bot = getattr(app.state, "bot", None)
        if bot is not None:
            await bot.session.close()

    return app


def main() -> None:
    settings = get_settings()
    install_access_log_filters()
    uvicorn.run(create_runtime(settings), host=settings.app_host, port=settings.app_port)


if __name__ == "__main__":
    main()
