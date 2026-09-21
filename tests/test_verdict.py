"""Absent evidence must read as an unhealthy replay, not a clean one."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "export_examples", ROOT / "scripts" / "export_examples.py"
)
assert _spec and _spec.loader
export_examples = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_examples)

S2_TRACES = {name: f"trace_{name}" for name in export_examples.REQUIRED_TRACES["S2"]}


def assertion_event(name: str, *, holds: bool, defect: bool = False) -> dict[str, Any]:
    return {
        "operation": "portal.save_exploration",
        "outcome": "ok" if holds else "assertion_failed",
        "assertion": {"name": name, "holds": holds, "known_baseline_defect": defect},
    }


def full_s2_events() -> list[dict[str, Any]]:
    return [
        assertion_event("discard_removes_the_exploration_immediately", holds=True),
        assertion_event(
            "discarded_exploration_link_stays_gone", holds=False, defect=True
        ),
        assertion_event(
            "new_exploration_does_not_reuse_a_discarded_key", holds=False, defect=True
        ),
    ]


def test_complete_replay_reports_defects_and_a_healthy_harness() -> None:
    summary = export_examples.verdict("S2", S2_TRACES, full_s2_events(), True)
    assert summary["harness_healthy"] is True
    assert summary["missing_evidence"] == []
    assert summary["baseline_defect_reproduced"] == [
        "discarded_exploration_link_stays_gone",
        "new_exploration_does_not_reuse_a_discarded_key",
    ]


def test_blank_trace_id_is_missing_evidence() -> None:
    summary = export_examples.verdict(
        "S2", {**S2_TRACES, "save_first": ""}, full_s2_events(), True
    )
    assert summary["harness_healthy"] is False
    assert "trace:save_first" in summary["missing_evidence"]


def test_absent_trace_key_is_missing_evidence() -> None:
    traces = {k: v for k, v in S2_TRACES.items() if k != "reopen_stale_link"}
    summary = export_examples.verdict("S2", traces, full_s2_events(), True)
    assert summary["harness_healthy"] is False
    assert "trace:reopen_stale_link" in summary["missing_evidence"]


def test_empty_replay_is_never_healthy() -> None:
    summary = export_examples.verdict("S2", {"save_first": ""}, [], True)
    assert summary["harness_healthy"] is False
    assert summary["baseline_defect_reproduced"] == []


def test_missing_contract_assertion_is_missing_evidence() -> None:
    events = [e for e in full_s2_events() if e["assertion"]["name"] != "discarded_exploration_link_stays_gone"]
    summary = export_examples.verdict("S2", S2_TRACES, events, True)
    assert summary["harness_healthy"] is False
    assert "assertion:discarded_exploration_link_stays_gone" in summary["missing_evidence"]


def test_unproven_fixture_reset_blocks_the_verdict() -> None:
    summary = export_examples.verdict("S2", S2_TRACES, full_s2_events(), False)
    assert summary["harness_healthy"] is False
    assert f"assertion:{export_examples.RESET_ASSERTION}" in summary["missing_evidence"]


def test_n1_needs_a_real_denial() -> None:
    traces = {name: f"trace_{name}" for name in export_examples.REQUIRED_TRACES["N1"]}
    without = export_examples.verdict("N1", traces, [], True)
    assert without["harness_healthy"] is False
    assert "outcome:expected_denial" in without["missing_evidence"]

    denied = [{"operation": "portal.save_exploration", "outcome": "expected_denial"}]
    assert export_examples.verdict("N1", traces, denied, True)["harness_healthy"] is True
