from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum

FORBIDDEN_KEYS = {
    "account",
    "authorization",
    "bot_token",
    "card",
    "cookie",
    "dsn",
    "hash",
    "init_data",
    "password",
    "payment_details",
    "raw_init_data",
    "raw_user",
    "token",
    "user_json",
}


def _level_to_number(level: str | int) -> int:
    if isinstance(level, int):
        return level
    normalized = level.upper()
    if normalized not in {"INFO", "WARNING", "ERROR"}:
        raise ValueError("unsupported log level")
    return getattr(logging, normalized)


def _safe_scalar(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for key, item in value.items():
            if key in FORBIDDEN_KEYS or item is None:
                continue
            sanitized[str(key)] = _safe_scalar(item)
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_scalar(item) for item in value if item is not None]
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def log_event(logger: logging.Logger, level: str | int, event: str, message: str, **fields) -> None:
    payload: dict[str, object] = {
        "event": event,
        "level": logging.getLevelName(_level_to_number(level)),
        "message": message,
    }
    for key, value in fields.items():
        if key in FORBIDDEN_KEYS or value is None:
            continue
        payload[str(key)] = _safe_scalar(value)
    logger.log(_level_to_number(level), json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
