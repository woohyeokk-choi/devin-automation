"""Outcome classification: a failed setup can never read as a product verdict."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from portal.config import Settings
from portal.domain import Portal, _sort_of
from portal.events import EventStore
from portal.tracing import Trace
from portal.upstream import UpstreamUnavailable


def _portal(tmp_path: Path) -> tuple[Portal, EventStore]:
    settings = Settings(
        superset_base_url="http://127.0.0.1:1",  # nothing listens here
        mcp_url="http://127.0.0.1:1/mcp",
        data_dir=tmp_path,
    )
    store = EventStore(tmp_path / "events.sqlite", stream=io.StringIO())
    return Portal(settings, store), store


def test_unreachable_superset_is_blocked_not_a_verdict(tmp_path: Path) -> None:
    portal, store = _portal(tmp_path)
    trace = portal.new_trace("analyst", scenario="S2")
    with pytest.raises(UpstreamUnavailable):
        portal.gateway(trace, "analyst")

    outcomes = [event["outcome"] for event in store.trace(trace.trace_id)]
    assert outcomes == ["blocked"]
    assert "assertion_failed" not in outcomes
    assert "ok" not in outcomes


def test_unmeasured_provenance_is_not_overclaimed(tmp_path: Path) -> None:
    portal, _ = _portal(tmp_path)
    assert portal.new_trace("analyst").revision["strength"] == "unmeasured"


def test_event_store_rejects_unknown_outcomes(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite", stream=io.StringIO())
    trace = Trace(
        store=store,
        actor="analyst",
        environment_kind="test",
        run_id="test",
        revision={},
    )
    with pytest.raises(ValueError):
        trace.log("assertion", "portal.thing", "verified")


def test_assertion_failure_is_not_turned_into_a_transport_error(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite", stream=io.StringIO())
    trace = Trace(
        store=store,
        actor="analyst",
        environment_kind="test",
        run_id="test",
        revision={},
    )
    holds = trace.assert_contract(
        "row_limit_survives_an_unrelated_change",
        expected=137,
        observed=1000,
        operation="portal.change_chart_sort",
    )
    event = store.trace(trace.trace_id)[0]
    assert holds is False
    assert event["outcome"] == "assertion_failed"
    assert event["http_status"] is None


def test_table_chart_sort_is_read_back_from_order_by_cols() -> None:
    assert _sort_of({"order_by_cols": ['["SUM(revenue)", true]']}) == ("SUM(revenue)", True)
    assert _sort_of({"timeseries_limit_metric": {"label": "SUM(revenue)"}, "order_desc": True}) == (
        "SUM(revenue)",
        False,
    )
    assert _sort_of({}) == (None, None)
