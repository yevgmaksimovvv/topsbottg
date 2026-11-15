"""Regression matrix: every sensitive admin route must enforce Telegram identity.

This also acts as a guard against accidentally introducing an unprotected
admin endpoint: the test compares the declared routes with this inventory.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from topsbottg.access import grant_admin, revoke_admin

ADMIN_ROUTES = [
    ("GET", "/api/admin/me", None),
    ("POST", "/api/admin/events-token", None),
    ("GET", "/api/admin/admin-grants", None),
    ("DELETE", "/api/admin/admin-grants/10", None),
    ("GET", "/api/admin/admin-candidates", None),
    ("GET", "/api/admin/admin-invitations", None),
    ("POST", "/api/admin/admin-invitations", {"target_telegram_id": 10}),
    ("POST", "/api/admin/admin-invitations/10/cancel", None),
    ("GET", "/api/admin/study-groups", None),
    ("GET", "/api/admin/users", None),
    ("GET", "/api/admin/users/10", None),
    ("GET", "/api/admin/users/10/payment-details", None),
    ("GET", "/api/admin/payouts", None),
    ("POST", "/api/admin/payouts", {
        "period_start_day": 1, "period_start_month": 11,
        "period_end_day": 7, "period_end_month": 11,
    }),
    ("GET", "/api/admin/payouts/10", None),
    ("DELETE", "/api/admin/payouts/10", None),
    ("PATCH", "/api/admin/payouts/10", {"message_before_period": "updated"}),
    ("POST", "/api/admin/payouts/10/recipients", {"user_ids": [10]}),
    ("DELETE", "/api/admin/payouts/10/recipients/20", None),
    ("POST", "/api/admin/payouts/10/send", None),
    ("POST", "/api/admin/payouts/10/send-selected", {"user_ids": [10]}),
    ("POST", "/api/admin/payouts/10/close", None),
    ("POST", "/api/admin/payouts/10/cancel", None),
    ("POST", "/api/admin/payouts/10/recipients/20/retry", None),
    ("GET", "/api/admin/payouts/10/recipients", None),
    ("POST", "/api/admin/payouts/10/recipients/20/mark-paid", {
        "expected_snapshot_version": 1,
    }),
]


def test_admin_route_inventory_is_complete(app):
    """A new admin endpoint requires an explicit access-control regression."""
    declared = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/admin/")
        for method in route.methods
    }
    covered = {
        (method, _route_template(path))
        for method, path, _body in ADMIN_ROUTES
    }
    # SSE access uses short-lived one-time tokens and has its own tests.
    assert declared - {("GET", "/api/admin/events")} == covered


def _route_template(path: str) -> str:
    parts = path.split("/")
    return "/".join("{user_id}" if value == "10" and parts[index - 1] == "users"
                    else "{telegram_id}" if value == "10" and parts[index - 1] == "admin-grants"
                    else "{invitation_id}" if value == "10" and parts[index - 1] == "admin-invitations"
                    else "{payout_id}" if value == "10" and parts[index - 1] == "payouts"
                    else "{recipient_id}" if value == "20" and parts[index - 1] == "recipients"
                    else value for index, value in enumerate(parts))


@pytest.mark.parametrize(
    "method,path,body",
    ADMIN_ROUTES,
    ids=[f"{method} {path}" for method, path, _ in ADMIN_ROUTES],
)
@pytest.mark.parametrize("authenticated,expected_status", [(False, 401), (True, 403)],
                         ids=["missing-init-data", "non-admin"])
async def test_admin_route_rejects_unauthorized_user(
    client, non_admin_init_data, method, path, body, authenticated, expected_status
):
    headers = {"X-Telegram-Init-Data": non_admin_init_data} if authenticated else {}
    response = await client.request(method, path, headers=headers, json=body)
    assert response.status_code == expected_status, (
        f"{method} {path}: expected {expected_status}, got "
        f"{response.status_code}: {response.text}"
    )


OWNER_ONLY_ROUTES = {
    ("GET", "/api/admin/admin-grants"),
    ("DELETE", "/api/admin/admin-grants/10"),
    ("GET", "/api/admin/admin-candidates"),
    ("GET", "/api/admin/admin-invitations"),
    ("POST", "/api/admin/admin-invitations"),
    ("POST", "/api/admin/admin-invitations/10/cancel"),
}


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
async def test_active_database_admin_can_use_non_owner_routes(
    client, session_factory, settings, dynamic_admin_init_data, method, path, body
):

    async with session_factory() as session:
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        await session.commit()
    init_data = dynamic_admin_init_data
    response = await client.request(
        method, path, headers={"X-Telegram-Init-Data": init_data}, json=body
    )
    if (method, path) in OWNER_ONLY_ROUTES:
        assert response.status_code == 403, (method, path, response.text)
    else:
        assert response.status_code not in (401, 403), (method, path, response.text)


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
async def test_revoked_database_admin_has_no_admin_api_access(
    client, session_factory, settings, dynamic_admin_init_data, method, path, body
):

    async with session_factory() as session:
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        await revoke_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set
        )
        await session.commit()
    init_data = dynamic_admin_init_data
    response = await client.request(
        method, path, headers={"X-Telegram-Init-Data": init_data}, json=body
    )
    assert response.status_code == 403, (method, path, response.text)
