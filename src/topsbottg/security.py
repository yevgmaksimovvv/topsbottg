from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl


@dataclass(slots=True)
class TelegramInitData:
    user_id: int
    auth_date: int
    raw_user: dict


class InitDataError(ValueError):
    pass


def validate_telegram_init_data(init_data: str, bot_token: str, *, max_age_seconds: int) -> TelegramInitData:
    if not init_data:
        raise InitDataError("initData is empty")

    params = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = params.pop("hash", None)
    if not received_hash:
        raise InitDataError("hash is missing")

    # Подпись проверяем на backend: данным с фронтенда доверять нельзя даже внутри Telegram WebApp.
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        raise InitDataError("invalid initData signature")

    if "user" not in params:
        raise InitDataError("user is missing")

    try:
        raw_user = json.loads(params["user"])
        user_id = int(raw_user["id"])
        auth_date = int(params["auth_date"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InitDataError("invalid user payload") from exc

    now = int(datetime.now(UTC).timestamp())
    age = now - auth_date
    if age < 0 or age > max_age_seconds:
        raise InitDataError("initData auth_date is expired")

    return TelegramInitData(user_id=user_id, auth_date=auth_date, raw_user=raw_user)
