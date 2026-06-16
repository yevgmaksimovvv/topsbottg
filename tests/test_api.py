from __future__ import annotations

import json
import logging
from typing import Literal

import pytest
from sqlalchemy import select

import topsbottg.api as api_module
from topsbottg.logging_utils import log_event
from topsbottg.models import AuditLog, RecipientStatus
from topsbottg.services import add_recipients, create_payout, get_or_create_user, upsert_payment_profile


class _FakeTelegramResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> _FakeTelegramResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        return False

    def read(self) -> bytes:
        return self._body


def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for record in caplog.records:
        try:
            events.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            continue
    return events


def _payout_payload(
    *,
    start_day: int = 1,
    start_month: int = 2,
    end_day: int = 28,
    end_month: int = 2,
    message_template: str | None = None,
) -> dict[str, object]:
    return {
        "period_start_day": start_day,
        "period_start_month": start_month,
        "period_end_day": end_day,
        "period_end_month": end_month,
        "message_template": message_template,
    }


def test_log_event_writes_valid_json_and_scrubs_forbidden_keys(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)

    log_event(
        logging.getLogger("topsbottg.tests"),
        "INFO",
        "sample_event",
        "Проверка",
        token="secret",
        payment_details="1111 1111 1111 1111",
        custom_object=object(),
        nested={"hash": "nope", "ok": 1},
        present=None,
    )

    payload = json.loads(caplog.records[0].getMessage())
    assert payload["event"] == "sample_event"
    assert payload["message"] == "Проверка"
    assert payload["level"] == "INFO"
    assert "token" not in payload
    assert "payment_details" not in payload
    assert payload["nested"] == {"ok": 1}
    assert isinstance(payload["custom_object"], str)


@pytest.mark.asyncio
async def test_check_telegram_ready_returns_true_for_success_response(monkeypatch: pytest.MonkeyPatch):
    def _urlopen(url, timeout):  # noqa: ANN001
        assert url == "https://api.telegram.org/bottest-token/getMe"
        assert timeout == 3.0
        return _FakeTelegramResponse(200, b'{"ok": true, "result": {"id": 1}}')

    monkeypatch.setattr(api_module.urllib.request, "urlopen", _urlopen)

    assert await api_module.check_telegram_ready("test-token") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (503, b'{"ok": true, "result": {"id": 1}}', False),
        (200, b"not-json", False),
        (200, b'{"ok": false, "result": {"id": 1}}', False),
    ],
)
async def test_check_telegram_ready_returns_false_for_failed_responses(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    expected: bool,
):
    def _urlopen(url, timeout):  # noqa: ANN001
        assert url == "https://api.telegram.org/bottest-token/getMe"
        assert timeout == 3.0
        return _FakeTelegramResponse(status, body)

    monkeypatch.setattr(api_module.urllib.request, "urlopen", _urlopen)

    assert await api_module.check_telegram_ready("test-token") is expected


@pytest.mark.asyncio
async def test_check_telegram_ready_returns_false_when_urlopen_raises(monkeypatch: pytest.MonkeyPatch):
    def _urlopen(url, timeout):  # noqa: ANN001
        raise OSError("boom")

    monkeypatch.setattr(api_module.urllib.request, "urlopen", _urlopen)

    assert await api_module.check_telegram_ready("test-token") is False


@pytest.mark.asyncio
async def test_healthz_returns_200_when_checks_ok(client, monkeypatch: pytest.MonkeyPatch):
    async def _telegram_ok(bot_token: str, timeout_seconds: float = 3.0) -> bool:
        return True

    async def _database_ok(session_factory) -> bool:  # noqa: ANN001
        return True

    monkeypatch.setattr(api_module, "check_database_ready", _database_ok)
    monkeypatch.setattr(api_module, "check_telegram_ready", _telegram_ok)
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "telegram": "ok"}}


@pytest.mark.asyncio
async def test_healthz_returns_503_when_database_check_fails(client, monkeypatch: pytest.MonkeyPatch):
    async def _database_failed(session_factory) -> bool:  # noqa: ANN001
        return False

    async def _telegram_ok(bot_token: str, timeout_seconds: float = 3.0) -> bool:
        return True

    monkeypatch.setattr(api_module, "check_database_ready", _database_failed)
    monkeypatch.setattr(api_module, "check_telegram_ready", _telegram_ok)
    response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "checks": {"database": "failed", "telegram": "ok"}}


@pytest.mark.asyncio
async def test_healthz_returns_503_when_telegram_check_fails(client, monkeypatch: pytest.MonkeyPatch):
    async def _telegram_failed(bot_token: str, timeout_seconds: float = 3.0) -> bool:
        return False

    async def _database_ok(session_factory) -> bool:  # noqa: ANN001
        return True

    monkeypatch.setattr(api_module, "check_database_ready", _database_ok)
    monkeypatch.setattr(api_module, "check_telegram_ready", _telegram_failed)
    response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "checks": {"database": "ok", "telegram": "failed"}}


@pytest.mark.asyncio
async def test_healthz_never_exposes_secrets(client, monkeypatch: pytest.MonkeyPatch, settings):
    async def _database_ok(session_factory) -> bool:  # noqa: ANN001
        return True

    async def _telegram_ok(bot_token: str, timeout_seconds: float = 3.0) -> bool:
        return True

    monkeypatch.setattr(api_module, "check_database_ready", _database_ok)
    monkeypatch.setattr(api_module, "check_telegram_ready", _telegram_ok)
    response = await client.get("/healthz")
    body = response.text
    assert settings.bot_token not in body
    assert settings.database_url not in body


@pytest.mark.asyncio
async def test_fresh_valid_init_data_accepted(client, admin_init_data, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    response = await client.get("/api/admin/me", headers={"X-Telegram-Init-Data": admin_init_data})
    assert response.status_code == 200
    assert response.json()["is_admin"] is True
    assert response.json()["telegram_user_id"] == 123
    events = _events(caplog)
    assert any(event.get("event") == "telegram_init_data_validation_ok" for event in events)
    assert any(event.get("event") == "admin_api_access_allowed" for event in events)


@pytest.mark.asyncio
async def test_non_admin_cannot_call_admin_api(client, non_admin_init_data):
    response = await client.get("/api/admin/me", headers={"X-Telegram-Init-Data": non_admin_init_data})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_missing_init_data_rejected(client, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    response = await client.get("/api/admin/me")
    assert response.status_code == 401
    events = _events(caplog)
    assert any(
        event.get("event") == "admin_api_access_denied" and event.get("reason") == "missing_init_data"
        for event in events
    )


@pytest.mark.asyncio
async def test_invalid_telegram_init_data_rejected(client, invalid_init_data):
    response = await client.get("/api/admin/me", headers={"X-Telegram-Init-Data": invalid_init_data})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_init_data_rejected(client, expired_init_data, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    response = await client.get("/api/admin/me", headers={"X-Telegram-Init-Data": expired_init_data})
    assert response.status_code == 401
    events = _events(caplog)
    failed = next(event for event in events if event.get("event") == "telegram_init_data_validation_failed")
    assert failed["reason"] == "expired_auth_date"
    assert "age_seconds" in failed
    assert "ttl_seconds" in failed


@pytest.mark.asyncio
async def test_missing_auth_date_rejected(client, missing_auth_date_init_data):
    response = await client.get("/api/admin/me", headers={"X-Telegram-Init-Data": missing_auth_date_init_data})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_users_search_keeps_same_full_names_separate(client, session_factory, admin_init_data):
    async with session_factory() as session:
        user_one = await get_or_create_user(session, 111, "Иван Иванов")
        _user_two = await get_or_create_user(session, 222, "Иван Иванов")
        await session.commit()

    response = await client.get(
        "/api/admin/users",
        params={"search": "Иван Иванов"},
        headers={"X-Telegram-Init-Data": admin_init_data},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["has_more"] is False
    assert len(body["items"]) == 2
    assert {row["telegram_user_id"] for row in body["items"]} == {111, 222}
    assert all("raw_payment_details" not in row for row in body["items"])
    assert all("is_active" not in row for row in body["items"])

    detail = await client.get(f"/api/admin/users/{user_one.id}", headers={"X-Telegram-Init-Data": admin_init_data})
    assert detail.status_code == 200
    assert detail.json()["user"]["telegram_user_id"] == 111
    assert detail.json()["user"]["full_name"] == "Иван Иванов"
    assert detail.json()["user"]["telegram_id"] == 111
    assert "is_active" not in detail.json()["user"]


@pytest.mark.asyncio
async def test_admin_users_ignore_is_active_query_param(client, session_factory, admin_init_data):
    async with session_factory() as session:
        await get_or_create_user(session, 111, "Иван Иванов")
        await session.commit()

    response = await client.get(
        "/api/admin/users",
        params={"is_active": "false"},
        headers={"X-Telegram-Init-Data": admin_init_data},
    )
    assert response.status_code == 200
    body = response.json()
    assert [row["telegram_user_id"] for row in body["items"]] == [111]
    assert "is_active" not in body["items"][0]


@pytest.mark.asyncio
async def test_admin_users_sort_by_full_name_then_id(client, session_factory, admin_init_data):
    async with session_factory() as session:
        await get_or_create_user(session, 111, "Иван Иванов")
        await get_or_create_user(session, 222, "Алексей Петров")
        await get_or_create_user(session, 333, "Иван Иванов")
        await session.commit()

    response = await client.get("/api/admin/users", headers={"X-Telegram-Init-Data": admin_init_data})
    assert response.status_code == 200
    rows = response.json()["items"]
    assert [row["full_name"] for row in rows[:3]] == ["Алексей Петров", "Иван Иванов", "Иван Иванов"]
    assert [row["telegram_user_id"] for row in rows[:3]] == [222, 111, 333]
    assert rows[1]["id"] < rows[2]["id"]


@pytest.mark.asyncio
async def test_admin_can_reveal_raw_payment_details(client, session_factory, admin_init_data):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await upsert_payment_profile(
            session,
            user,
            "Сбер\n+7 999 123 45 67\nИван",
        )
        await session.commit()

    response = await client.get(
        f"/api/admin/users/{user.id}/payment-details",
        headers={"X-Telegram-Init-Data": admin_init_data},
    )
    assert response.status_code == 200
    assert response.json()["raw_payment_details"] == "Сбер\n+7 999 123 45 67\nИван"

    async with session_factory() as session:
        rows = await session.scalars(select(AuditLog).where(AuditLog.action == "view_payment_details"))
        log = rows.first()
        assert log is not None
        assert log.meta in (None, {})


@pytest.mark.asyncio
async def test_admin_users_pagination_and_cache_headers(client, session_factory, admin_init_data):
    async with session_factory() as session:
        for telegram_id in (111, 222, 333):
            await get_or_create_user(session, telegram_id, f"User {telegram_id}")
        await session.commit()

    response = await client.get(
        "/api/admin/users",
        params={"limit": 2, "offset": 0},
        headers={"X-Telegram-Init-Data": admin_init_data},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert body["has_more"] is True
    assert len(body["items"]) == 2


@pytest.mark.asyncio
async def test_admin_reveal_payment_details_missing_user_or_profile(client, session_factory, admin_init_data):
    response = await client.get(
        "/api/admin/users/999/payment-details",
        headers={"X-Telegram-Init-Data": admin_init_data},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "user not found"

    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await session.commit()

    response = await client.get(
        f"/api/admin/users/{user.id}/payment-details",
        headers={"X-Telegram-Init-Data": admin_init_data},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "payment details not found"


@pytest.mark.asyncio
async def test_admin_reveal_payment_details_requires_admin(client, non_admin_init_data):
    response = await client.get(
        "/api/admin/users/1/payment-details",
        headers={"X-Telegram-Init-Data": non_admin_init_data},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_send_marks_payout_sending(client, session_factory, admin_init_data, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    async with session_factory() as session:
        await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        await session.commit()
    response = await client.post("/api/admin/payouts/1/send", headers={"X-Telegram-Init-Data": admin_init_data})
    assert response.status_code == 200
    assert response.json()["status"] == "sending"
    events = _events(caplog)
    assert any(event.get("event") == "payout_send_requested" for event in events)
    completed = next(
        event
        for event in events
        if event.get("event") == "payout_action_completed" and event.get("action") == "send_payout"
    )
    assert completed["action"] == "send_payout"
    assert completed["payout_id"] == 1
    assert "raw_payment_details" not in json.dumps(completed, ensure_ascii=False)


@pytest.mark.asyncio
async def test_payout_status_cannot_be_patched_arbitrarily(client, session_factory, admin_init_data):
    async with session_factory() as session:
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        await session.commit()
    response = await client.patch(
        f"/api/admin/payouts/{payout.id}",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={"status": "completed"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_mark_paid_rejects_wrong_status(client, session_factory, admin_init_data):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        await session.commit()
    response = await client.post(
        f"/api/admin/payouts/{payout.id}/recipients/{recipient.id}/mark-paid",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_mark_paid_endpoint(client, session_factory, admin_init_data):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.payment_received.value
        await session.commit()
    response = await client.post(
        f"/api/admin/payouts/{payout.id}/recipients/{recipient.id}/mark-paid",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={"paid_note": "done"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "paid"
    assert payload["paid_by_admin_id"] == 123


@pytest.mark.asyncio
async def test_payout_validation_rejects_bad_dates_and_placeholders(client, session_factory, admin_init_data):
    response = await client.post(
        "/api/admin/payouts",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={
            "period_start_day": 31,
            "period_start_month": 2,
            "period_end_day": 1,
            "period_end_month": 2,
            "message_template": None,
        },
    )
    assert response.status_code == 400
    assert "невозможная дата" in response.json()["detail"]

    response = await client.post(
        "/api/admin/payouts",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={
            "period_start_day": 1,
            "period_start_month": 2,
            "period_end_day": 28,
            "period_end_month": 2,
            "message_template": "hello {name}",
        },
    )
    assert response.status_code == 400
    assert "шаблон" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_payout_uses_yearless_period_response(client, admin_init_data):
    response = await client.post(
        "/api/admin/payouts",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={
            "period_start_day": 5,
            "period_start_month": 6,
            "period_end_day": 5,
            "period_end_month": 6,
            "message_template": None,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert "title" not in payload
    assert "period_from" not in payload
    assert "period_to" not in payload
    assert "period_start_day" in payload
    assert payload["period_label"] == "05.06 — 05.06"


@pytest.mark.asyncio
async def test_create_payout_rejects_invalid_month_and_day(client, admin_init_data):
    response = await client.post(
        "/api/admin/payouts",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={
            "period_start_day": 1,
            "period_start_month": 13,
            "period_end_day": 5,
            "period_end_month": 6,
            "message_template": None,
        },
    )
    assert response.status_code == 400
    assert "Месяц начала" in response.json()["detail"]

    response = await client.post(
        "/api/admin/payouts",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={
            "period_start_day": 0,
            "period_start_month": 6,
            "period_end_day": 5,
            "period_end_month": 6,
            "message_template": None,
        },
    )
    assert response.status_code == 400
    assert "День начала" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_payout_accepts_29_february(client, admin_init_data):
    response = await client.post(
        "/api/admin/payouts",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={
            "period_start_day": 29,
            "period_start_month": 2,
            "period_end_day": 29,
            "period_end_month": 2,
            "message_template": None,
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_payouts_returns_yearless_labels(client, admin_init_data):
    await client.post(
        "/api/admin/payouts",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={
            "period_start_day": 5,
            "period_start_month": 6,
            "period_end_day": 5,
            "period_end_month": 6,
            "message_template": None,
        },
    )
    await client.post(
        "/api/admin/payouts",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={
            "period_start_day": 5,
            "period_start_month": 6,
            "period_end_day": 5,
            "period_end_month": 6,
            "message_template": None,
        },
    )
    response = await client.get("/api/admin/payouts", headers={"X-Telegram-Init-Data": admin_init_data})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2
    assert payload[0]["period_label"] == "05.06 — 05.06"
    assert "title" not in payload[0]


@pytest.mark.asyncio
async def test_admin_cancel_completed_payout_forbidden(client, session_factory, admin_init_data):
    async with session_factory() as session:
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        payout.status = "completed"
        await session.commit()
    response = await client.post(
        f"/api/admin/payouts/{payout.id}/cancel", headers={"X-Telegram-Init-Data": admin_init_data}
    )
    assert response.status_code == 400
