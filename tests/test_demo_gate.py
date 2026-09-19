"""The demo gate: no anonymous or cross-origin request may reach upstream.

An event is the raw material of a repair job, so the portal must not mint one
on behalf of a caller it cannot attribute. Each rejection below is checked
twice: the HTTP status, and that the event store gained nothing — a rejected
request that still logged an action would be an event a later incident could
be built from.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from portal import app as portal_app
from portal.config import settings
from portal.security import CSRF_COOKIE, PROFILE_COOKIE, sign_profile

AUTH = (settings.demo_username, settings.demo_password)
OPS = (settings.ops_username, settings.ops_password)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(portal_app.app)


def _event_count() -> int:
    return len(portal_app.store.recent(10_000))


def _authenticated(client: TestClient) -> str:
    """Log in through the gate and return a usable CSRF token."""
    response = client.get("/", auth=AUTH)
    assert response.status_code == 200
    return client.cookies[CSRF_COOKIE]


WRITES: list[tuple[str, dict[str, Any]]] = [
    ("/profile", {"profile": "restricted_viewer"}),
    ("/tabs/new", {}),
    ("/explorations", {"dimension": "region", "sort": "desc", "row_limit": 50}),
    ("/explorations/anykey/discard", {}),
    ("/settings/sort", {"descending": "false"}),
]


@pytest.mark.parametrize("path,form", WRITES)
def test_anonymous_writes_are_rejected_and_log_nothing(
    client: TestClient, path: str, form: dict[str, Any]
) -> None:
    before = _event_count()
    response = client.post(path, data=form)
    assert response.status_code == 401
    assert _event_count() == before


@pytest.mark.parametrize("path", ["/", "/settings", "/explorations/anykey", "/ops"])
def test_anonymous_reads_are_rejected(client: TestClient, path: str) -> None:
    before = _event_count()
    assert client.get(path).status_code == 401
    assert _event_count() == before


def test_health_check_stays_open(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("path,form", WRITES)
def test_authenticated_write_without_a_form_token_is_rejected(
    client: TestClient, path: str, form: dict[str, Any]
) -> None:
    _authenticated(client)
    before = _event_count()
    response = client.post(path, data=form, auth=AUTH)
    assert response.status_code == 403
    assert _event_count() == before


def test_cross_origin_write_is_rejected(client: TestClient) -> None:
    token = _authenticated(client)
    before = _event_count()
    response = client.post(
        "/explorations",
        data={"dimension": "region", "sort": "desc", "row_limit": 50, "csrf_token": token},
        headers={"Origin": "https://attacker.example"},
        auth=AUTH,
    )
    assert response.status_code == 403
    assert _event_count() == before


def test_stale_form_token_is_rejected(client: TestClient) -> None:
    _authenticated(client)
    before = _event_count()
    response = client.post(
        "/tabs/new", data={"csrf_token": "not-the-issued-token"}, auth=AUTH
    )
    assert response.status_code == 403
    assert _event_count() == before


def test_unknown_profile_is_refused_not_downgraded(client: TestClient) -> None:
    token = _authenticated(client)
    response = client.post(
        "/profile", data={"profile": "superuser", "csrf_token": token}, auth=AUTH
    )
    assert response.status_code == 400
    assert PROFILE_COOKIE not in response.cookies


def test_forged_profile_cookie_is_refused(client: TestClient) -> None:
    # Unsigned, as a client that simply wrote the cookie value would send it.
    response = client.get("/", auth=AUTH, cookies={PROFILE_COOKIE: "analyst"})
    assert response.status_code == 400


def test_known_profile_is_accepted_and_signed(client: TestClient) -> None:
    token = _authenticated(client)
    response = client.post(
        "/profile",
        data={"profile": "restricted_viewer", "csrf_token": token},
        auth=AUTH,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.cookies[PROFILE_COOKIE] == sign_profile("restricted_viewer")


def test_demo_user_cannot_reach_the_operator_console(client: TestClient) -> None:
    assert client.get("/ops", auth=AUTH).status_code == 401
    assert client.get("/ops", auth=OPS).status_code == 200


def test_fixture_reset_needs_the_operator_and_a_form_token(client: TestClient) -> None:
    before = _event_count()
    assert client.post("/ops/fixtures/reset", auth=AUTH).status_code == 403
    assert client.post("/ops/fixtures/reset", auth=OPS).status_code == 403
    assert _event_count() == before


def test_the_incident_console_is_operator_only(client: TestClient) -> None:
    for path in ("/ops/incidents", "/ops/incidents/1", "/ops/incidents/1/files/incident.json"):
        assert client.get(path).status_code == 401
        assert client.get(path, auth=AUTH).status_code == 401
    assert client.get("/ops/incidents", auth=OPS).status_code == 200
    assert client.get("/ops/incidents/999999", auth=OPS).status_code == 404


def test_writing_a_bundle_needs_the_operator_and_a_form_token(client: TestClient) -> None:
    assert client.post("/ops/incidents/1/export", auth=AUTH).status_code == 403
    assert client.post("/ops/incidents/1/export", auth=OPS).status_code == 403
