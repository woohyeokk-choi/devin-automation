"""A presentation deployment: its own chart, its own row limit, no rewriting.

The live opening of the demo shows a saved chart losing a row limit nobody
touched. That is only honest if the portal really omits the field, resets only
the chart this deployment owns, and points the viewer at the product's own
page rather than at its own reading of it.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portal import app as portal_app
from portal.config import Settings
from portal.domain import Portal
from portal.events import EventStore
from portal.security import CSRF_COOKIE

OPS = (portal_app.settings.ops_username, portal_app.settings.ops_password)


class FakeMCP:
    """Records what would have been sent, and answers as the product does."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self, trace: Any, tool: str, arguments: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        self.calls.append((tool, arguments))
        return {"chart": {"id": 42}}


class FakeGateway:
    """Superset as far as chart lookup is concerned: nothing saved yet."""

    profile = "analyst"

    def call(self, *_: Any, **__: Any) -> tuple[int, dict[str, Any]]:
        return 200, {"result": []}


def _portal(tmp_path: Path, **overrides: Any) -> tuple[Portal, FakeMCP]:
    settings = Settings(
        superset_base_url="http://127.0.0.1:1",
        mcp_url="http://127.0.0.1:1/mcp",
        data_dir=tmp_path,
        chart_name="Order revenue - live demonstration",
        **overrides,
    )
    portal = Portal(settings, EventStore(tmp_path / "events.sqlite", stream=io.StringIO()))
    mcp = FakeMCP()
    portal.mcp = mcp  # type: ignore[assignment]
    portal.read_chart = lambda trace, gateway, chart_id: {  # type: ignore[assignment]
        "id": chart_id,
        "name": settings.chart_name,
        "row_limit": settings.chart_row_limit,
        "color_scheme": "googleCategory10c",
        "sort_by": "SUM(revenue)",
        "order_desc": True,
        "params": {},
    }
    return portal, mcp


def test_the_presentation_chart_is_created_at_its_configured_row_limit(
    tmp_path: Path,
) -> None:
    portal, mcp = _portal(tmp_path, chart_row_limit=10)
    trace = portal.new_trace("analyst", scenario="S1")
    portal.ensure_chart(trace, FakeGateway(), dataset_id=1)  # type: ignore[arg-type]

    tool, arguments = mcp.calls[0]
    assert tool == "generate_chart"
    assert arguments["chart_name"] == "Order revenue - live demonstration"
    assert arguments["config"]["row_limit"] == 10


def test_the_default_row_limit_is_unchanged_for_every_other_deployment(
    tmp_path: Path,
) -> None:
    portal, mcp = _portal(tmp_path)
    trace = portal.new_trace("analyst", scenario="S1")
    portal.ensure_chart(trace, FakeGateway(), dataset_id=1)  # type: ignore[arg-type]
    assert mcp.calls[0][1]["config"]["row_limit"] == 137


def test_preparing_the_fixture_restores_the_configured_row_limit(
    tmp_path: Path,
) -> None:
    portal, mcp = _portal(tmp_path, chart_row_limit=10)
    trace = portal.new_trace("operator")
    portal.restore_fixture(trace, FakeGateway(), dataset_id=1)  # type: ignore[arg-type]

    tool, arguments = mcp.calls[-1]
    assert tool == "update_chart"
    assert arguments["config"]["row_limit"] == 10
    assert [
        event["assertion"]["holds"]
        for event in portal.store.trace(trace.trace_id)
        if event.get("assertion")
    ] == [True]


def test_a_sort_only_change_sends_no_row_limit_at_all(tmp_path: Path) -> None:
    portal, mcp = _portal(tmp_path, chart_row_limit=10)
    trace = portal.new_trace("analyst", scenario="S1")
    chart = {"id": 42, "row_limit": 10, "color_scheme": "googleCategory10c"}
    portal.change_chart_sort(trace, FakeGateway(), chart, descending=True)  # type: ignore[arg-type]

    tool, arguments = mcp.calls[-1]
    assert tool == "update_chart"
    assert "row_limit" not in arguments["config"]
    assert arguments["config"]["sort_by"] == [
        {"column": "SUM(revenue)", "ascending": False}
    ]


def test_a_raw_row_chart_sorts_on_the_plain_column_and_still_sends_no_limit(
    tmp_path: Path,
) -> None:
    portal, mcp = _portal(tmp_path, chart_row_limit=10, chart_query_mode="raw")
    trace = portal.new_trace("analyst", scenario="S1")
    chart = {"id": 42, "row_limit": 10, "color_scheme": "googleCategory10c"}
    portal.change_chart_sort(trace, FakeGateway(), chart, descending=False)  # type: ignore[arg-type]

    config = mcp.calls[-1][1]["config"]
    assert config["query_mode"] == "raw"
    assert "row_limit" not in config
    assert config["sort_by"] == [{"column": "revenue", "ascending": True}]
    assert [column["name"] for column in config["columns"]] == [
        "id",
        "region",
        "channel",
        "product",
        "revenue",
    ]


def test_a_raw_row_chart_is_created_and_reset_in_its_own_shape(tmp_path: Path) -> None:
    portal, mcp = _portal(tmp_path, chart_row_limit=10, chart_query_mode="raw")
    trace = portal.new_trace("operator")
    portal.ensure_chart(trace, FakeGateway(), dataset_id=1)  # type: ignore[arg-type]
    portal.restore_fixture(trace, FakeGateway(), dataset_id=1)  # type: ignore[arg-type]

    created = mcp.calls[0][1]["config"]
    reset = mcp.calls[-1][1]["config"]
    assert created["query_mode"] == reset["query_mode"] == "raw"
    assert created["row_limit"] == reset["row_limit"] == 10
    assert reset["sort_by"] == [{"column": "revenue", "ascending": False}]


def test_the_page_shows_the_request_the_portal_actually_sends(tmp_path: Path) -> None:
    portal, mcp = _portal(tmp_path, chart_row_limit=10, chart_query_mode="raw")
    trace = portal.new_trace("analyst", scenario="S1")
    chart = {"id": 42, "row_limit": 10, "color_scheme": "googleCategory10c"}
    portal.change_chart_sort(trace, FakeGateway(), chart, descending=True)  # type: ignore[arg-type]

    shown = portal.sort_only_request(42, descending=True)
    sent = dict(mcp.calls[-1][1])
    shown["config"]["sort_by"] = sent["config"]["sort_by"]
    assert shown == sent


def test_the_native_chart_link_is_offered_only_when_the_address_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chart = {"id": 42}
    assert portal_app.native_chart_url(None) == ""
    assert portal_app.native_chart_url(chart) == ""
    monkeypatch.setattr(
        portal_app,
        "settings",
        Settings(superset_public_url="http://127.0.0.1:8088/"),
    )
    assert (
        portal_app.native_chart_url(chart) == "http://127.0.0.1:8088/explore/?slice_id=42"
    )


def test_the_reset_button_cannot_be_turned_into_an_open_redirect() -> None:
    client = TestClient(portal_app.app)
    assert client.get("/ops", auth=OPS).status_code == 200
    token = client.cookies[CSRF_COOKIE]
    response = client.post(
        "/ops/fixtures/reset",
        data={"csrf_token": token, "back_to": "https://attacker.example"},
        auth=OPS,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ops?")

    response = client.post(
        "/ops/fixtures/reset",
        data={"csrf_token": token, "back_to": "/settings"},
        auth=OPS,
        follow_redirects=False,
    )
    assert response.headers["location"].startswith("/settings?")
