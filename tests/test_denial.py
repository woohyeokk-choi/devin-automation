"""A refused permission and a lost session are different observations.

N1 is only a control result when an *authenticated* restricted user is told
no. HTTP 401 means the login never took or expired, so nothing the run
observed afterwards says anything about what the role may do.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from portal.config import Settings
from portal.domain import ExplorationSpec, Portal
from portal.events import EventStore
from portal.tracing import Trace
from portal.upstream import NotAuthenticated, SupersetGateway


class StubResponse:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.content = b"{}"

    def json(self) -> dict[str, Any]:
        return {"message": "stub"}


class StubClient:
    """A Superset client that answers every call with one fixed status."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def login(self, username: str, password: str) -> None:
        del username, password

    def request(self, method: str, path: str, **kwargs: Any) -> StubResponse:
        del kwargs
        self.calls.append((method, path))
        return StubResponse(self.status)


def _trace(store: EventStore) -> Trace:
    return Trace(
        store=store,
        actor="restricted_viewer",
        environment_kind="test",
        run_id="test",
        revision={},
    )


def _gateway(status: int, profile: str) -> SupersetGateway:
    gateway = SupersetGateway(Settings(), profile)
    gateway.client = StubClient(status)  # type: ignore[assignment]
    return gateway


@pytest.mark.parametrize(
    ("profile", "status", "outcome"),
    [
        ("restricted_viewer", 403, "expected_denial"),
        ("restricted_viewer", 401, "blocked"),
        ("analyst", 403, "error"),
        ("analyst", 401, "blocked"),
    ],
)
def test_the_gateway_separates_a_refused_permission_from_a_lost_session(
    tmp_path: Path, profile: str, status: int, outcome: str
) -> None:
    store = EventStore(tmp_path / "events.sqlite", stream=io.StringIO())
    trace = _trace(store)
    gateway = _gateway(status, profile)
    observed, _ = gateway.call(
        trace, "portal.save_exploration", "POST", "/api/v1/explore/form_data"
    )
    assert observed == status
    logged = trace.store.recent(10)
    assert logged[0]["outcome"] == outcome


def test_a_restricted_save_that_is_denied_is_a_control_result(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite", stream=io.StringIO())
    trace = _trace(store)
    portal = Portal(Settings(), store)
    key, status = portal.save_exploration(
        trace,
        _gateway(403, "restricted_viewer"),
        dataset_id=1,
        spec=ExplorationSpec(dimension="region", sort="highest_first"),
        tab_id="tab-1",
    )
    assert (key, status) == (None, 403)
    assertions = [e for e in trace.store.recent(10) if e["kind"] == "assertion"]
    assert assertions[0]["outcome"] == "expected_denial"
    assert assertions[0]["assertion"]["holds"] is True


def test_a_restricted_save_without_a_session_is_an_environment_failure(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "events.sqlite", stream=io.StringIO())
    trace = _trace(store)
    portal = Portal(Settings(), store)
    with pytest.raises(NotAuthenticated) as raised:
        portal.save_exploration(
            trace,
            _gateway(401, "restricted_viewer"),
            dataset_id=1,
            spec=ExplorationSpec(dimension="region", sort="highest_first"),
            tab_id="tab-1",
        )
    detail = raised.value.safe_detail()
    assert detail["classification"] == "authentication_failure"
    assert not [
        event
        for event in trace.store.recent(10)
        if event["outcome"] == "expected_denial"
    ]
