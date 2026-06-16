from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl

from topsbottg.logging_utils import log_event

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TelegramInitData:
    user_id: int
    auth_date: int
    raw_user: dict


class InitDataError(ValueError):
    pass


def validate_telegram_init_data(init_data: str, bot_token: str, *, max_age_seconds: int) -> TelegramInitData:
    server_now = int(datetime.now(UTC).timestamp())
    has_init_data = bool(init_data)
    if not init_data:
        log_event(
            logger,
            "WARNING",
            "telegram_init_data_validation_failed",
            "Не передан initData",
            reason="missing_init_data",
            has_init_data=has_init_data,
            has_hash=False,
            has_user=False,
            has_auth_date=False,
            server_now=server_now,
            ttl_seconds=max_age_seconds,
        )
        raise InitDataError("initData is empty")

    params = dict(parse_qsl(init_data, keep_blank_values=True))
    has_hash = "hash" in params and bool(params.get("hash"))
    has_user = "user" in params and bool(params.get("user"))
    has_auth_date = "auth_date" in params and bool(params.get("auth_date"))
    received_hash = params.pop("hash", None)
    if not received_hash:
        log_event(
            logger,
            "WARNING",
            "telegram_init_data_validation_failed",
            "Отсутствует hash",
            reason="missing_hash",
            has_init_data=has_init_data,
            has_hash=has_hash,
            has_user=has_user,
            has_auth_date=has_auth_date,
            server_now=server_now,
            ttl_seconds=max_age_seconds,
        )
        raise InitDataError("hash is missing")

    # Подпись проверяем на backend: данным с фронтенда доверять нельзя даже внутри Telegram WebApp.
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        log_event(
            logger,
            "WARNING",
            "telegram_init_data_validation_failed",
            "Некорректная подпись initData",
            reason="bad_hash",
            has_init_data=has_init_data,
            has_hash=has_hash,
            has_user=has_user,
            has_auth_date=has_auth_date,
            server_now=server_now,
            ttl_seconds=max_age_seconds,
        )
        raise InitDataError("invalid initData signature")

    if "user" not in params:
        log_event(
            logger,
            "WARNING",
            "telegram_init_data_validation_failed",
            "Отсутствует user",
            reason="missing_user",
            has_init_data=has_init_data,
            has_hash=has_hash,
            has_user=has_user,
            has_auth_date=has_auth_date,
            server_now=server_now,
            ttl_seconds=max_age_seconds,
        )
        raise InitDataError("user is missing")

    try:
        raw_user = json.loads(params["user"])
        user_id = int(raw_user["id"])
    except json.JSONDecodeError as exc:
        log_event(
            logger,
            "WARNING",
            "telegram_init_data_validation_failed",
            "Некорректный user",
            reason="malformed_user",
            has_init_data=has_init_data,
            has_hash=has_hash,
            has_user=has_user,
            has_auth_date=has_auth_date,
            server_now=server_now,
            ttl_seconds=max_age_seconds,
        )
        raise InitDataError("invalid user payload") from exc
    except (KeyError, TypeError, ValueError) as exc:
        log_event(
            logger,
            "WARNING",
            "telegram_init_data_validation_failed",
            "Некорректный user",
            reason="malformed_user",
            has_init_data=has_init_data,
            has_hash=has_hash,
            has_user=has_user,
            has_auth_date=has_auth_date,
            server_now=server_now,
            ttl_seconds=max_age_seconds,
        )
        raise InitDataError("invalid user payload") from exc

    if "auth_date" not in params:
        log_event(
            logger,
            "WARNING",
            "telegram_init_data_validation_failed",
            "Отсутствует auth_date",
            reason="missing_auth_date",
            has_init_data=has_init_data,
            has_hash=has_hash,
            has_user=has_user,
            has_auth_date=has_auth_date,
            server_now=server_now,
            ttl_seconds=max_age_seconds,
        )
        raise InitDataError("invalid user payload")

    try:
        parsed_auth_date = int(params["auth_date"])
    except (TypeError, ValueError) as exc:
        log_event(
            logger,
            "WARNING",
            "telegram_init_data_validation_failed",
            "Некорректный auth_date",
            reason="malformed_auth_date",
            has_init_data=has_init_data,
            has_hash=has_hash,
            has_user=has_user,
            has_auth_date=has_auth_date,
            server_now=server_now,
            ttl_seconds=max_age_seconds,
        )
        raise InitDataError("invalid user payload") from exc

    age = server_now - parsed_auth_date
    if age < 0 or age > max_age_seconds:
        log_event(
            logger,
            "WARNING",
            "telegram_init_data_validation_failed",
            "initData устарел",
            reason="expired_auth_date",
            has_init_data=has_init_data,
            has_hash=has_hash,
            has_user=has_user,
            has_auth_date=has_auth_date,
            auth_date=parsed_auth_date,
            server_now=server_now,
            age_seconds=age,
            ttl_seconds=max_age_seconds,
        )
        raise InitDataError("initData auth_date is expired")

    log_event(
        logger,
        "INFO",
        "telegram_init_data_validation_ok",
        "Telegram initData успешно проверен",
        has_hash=has_hash,
        has_user=has_user,
        has_auth_date=has_auth_date,
        auth_date=parsed_auth_date,
        server_now=server_now,
        age_seconds=age,
        ttl_seconds=max_age_seconds,
        telegram_user_id=user_id,
    )
    return TelegramInitData(user_id=user_id, auth_date=parsed_auth_date, raw_user=raw_user)
