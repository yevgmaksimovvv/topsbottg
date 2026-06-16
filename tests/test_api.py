from __future__ import annotations

from datetime import datetime
from typing import Literal

import pytest
from sqlalchemy import select

import topsbottg.api as api_module
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
async def test_fresh_valid_init_data_accepted(client, admin_init_data):
    response = await client.get("/api/admin/me", headers={"X-Telegram-Init-Data": admin_init_data})
    assert response.status_code == 200
    assert response.json()["is_admin"] is True
    assert response.json()["telegram_user_id"] == 123


@pytest.mark.asyncio
async def test_non_admin_cannot_call_admin_api(client, non_admin_init_data):
    response = await client.get("/api/admin/me", headers={"X-Telegram-Init-Data": non_admin_init_data})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_missing_init_data_rejected(client):
    response = await client.get("/api/admin/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_invalid_telegram_init_data_rejected(client, invalid_init_data):
    response = await client.get("/api/admin/me", headers={"X-Telegram-Init-Data": invalid_init_data})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_init_data_rejected(client, expired_init_data):
    response = await client.get("/api/admin/me", headers={"X-Telegram-Init-Data": expired_init_data})
    assert response.status_code == 401


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

    detail = await client.get(f"/api/admin/users/{user_one.id}", headers={"X-Telegram-Init-Data": admin_init_data})
    assert detail.status_code == 200
    assert detail.json()["user"]["telegram_user_id"] == 111
    assert detail.json()["user"]["full_name"] == "Иван Иванов"
    assert detail.json()["user"]["telegram_id"] == 111


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
async def test_send_marks_payout_sending(client, session_factory, admin_init_data):
    async with session_factory() as session:
        await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Май",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        await session.commit()
    response = await client.post("/api/admin/payouts/1/send", headers={"X-Telegram-Init-Data": admin_init_data})
    assert response.status_code == 200
    assert response.json()["status"] == "sending"


@pytest.mark.asyncio
async def test_admin_csv_export_is_no_store(client, session_factory, admin_init_data):
    async with session_factory() as session:
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Май",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        await session.commit()
    response = await client.get(
        f"/api/admin/payouts/{payout.id}/export.csv",
        headers={"X-Telegram-Init-Data": admin_init_data},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_payout_status_cannot_be_patched_arbitrarily(client, session_factory, admin_init_data):
    async with session_factory() as session:
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Май",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        await session.commit()
    response = await client.patch(
        f"/api/admin/payouts/{payout.id}",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={"status": "completed"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "draft"


@pytest.mark.asyncio
async def test_mark_paid_rejects_wrong_status(client, session_factory, admin_init_data):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Май",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
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
            payload={
                "title": "Май",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
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
            "title": "Bad",
            "period_from": "2026-02-28",
            "period_to": "2026-02-01",
            "message_template": None,
        },
    )
    assert response.status_code == 400
    assert "period_from" in response.json()["detail"]

    response = await client.post(
        "/api/admin/payouts",
        headers={"X-Telegram-Init-Data": admin_init_data},
        json={
            "title": "Bad",
            "period_from": "2026-02-01",
            "period_to": "2026-02-28",
            "message_template": "hello {name}",
        },
    )
    assert response.status_code == 400
    assert "message_template" in response.json()["detail"] or "period_from" in response.json()["detail"]


@pytest.mark.asyncio
async def test_csv_export_contains_snapshot_fields(client, session_factory, admin_init_data):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Май",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.payment_received.value
        recipient.payment_profile_snapshot = {
            "raw_payment_details": "Иван Иванов\n+79990000000\nT-Bank\nnote",
        }
        await session.commit()
    response = await client.get(
        f"/api/admin/payouts/{payout.id}/export.csv",
        headers={"X-Telegram-Init-Data": admin_init_data},
    )
    assert response.status_code == 200
    body = response.text
    assert "raw_payment_details" in body
    assert "Иван Иванов" in body
    assert "T-Bank" in body


@pytest.mark.asyncio
async def test_csv_export_handles_empty_payout(client, session_factory, admin_init_data):
    async with session_factory() as session:
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Пустая",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        await session.commit()
    response = await client.get(
        f"/api/admin/payouts/{payout.id}/export.csv", headers={"X-Telegram-Init-Data": admin_init_data}
    )
    assert response.status_code == 200
    assert "recipient_id,payout_id,user_id" in response.text


@pytest.mark.asyncio
async def test_admin_cancel_completed_payout_forbidden(client, session_factory, admin_init_data):
    async with session_factory() as session:
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Май",
                "period_from": datetime(2026, 2, 1).date(),
                "period_to": datetime(2026, 2, 28).date(),
                "message_template": None,
            },
        )
        payout.status = "completed"
        await session.commit()
    response = await client.post(
        f"/api/admin/payouts/{payout.id}/cancel", headers={"X-Telegram-Init-Data": admin_init_data}
    )
    assert response.status_code == 400
