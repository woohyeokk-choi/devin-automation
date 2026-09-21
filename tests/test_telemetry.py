"""Browser telemetry: what qualifies, what is only visible, what is refused.

The trigger for a B repair is the product's own console output, so these tests
police the seam between "the page said something" and "a repair session may be
asked for". The console lines used here are the ones actually captured from
the baseline during reproduction; nothing is emitted by us.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from portal import telemetry
from portal.events import TELEMETRY, EventStore
from portal.incidents import DISABLED, DRY_RUN, ENABLED, BLOCKED, ELIGIBLE, IncidentStore

REPO = "woohyeokk-choi/superset"
BASELINE = "394bca55c792b7b3547e23f6e175a7cb0f0757e8"
REVISION = {
    "checkout_sha": BASELINE,
    "checkout_matches_running_code": True,
    "strength": "measured",
    "fixture_revision": "sha256:b1fixture0001",
}

# Verbatim from the baseline reproduction (run1/run2/run3 evidence).
WARNING = (
    "Formatter failed, falling back to raw value TypeError: Cannot convert a "
    "BigInt value to a number\n"
    "    at Math.abs (<anonymous>)\n"
    "    at Function.formatFunc (http://127.0.0.1:8588/static/assets/"
    "chunk.js?v=1:2:3)"
)
PAGE = {
    "chart": "repro44007 — byte counters (Table)",
    "route": "/explore/?slice_id=1",
    "visible_values": "1425300509404304697 | 4KiB",
}


def console(severity: str = "warning", text: str = WARNING, url: str = "") -> dict[str, Any]:
    return {
        "ts": "2026-09-20T10:00:00.000+00:00",
        "severity": severity,
        "text": text,
        "location": {"url": url or "http://127.0.0.1:8588/static/assets/chunk.js?v=1"},
    }


def telemetry_event(
    finding: telemetry.Finding,
    *,
    event_id: str = "t1",
    trace_id: str = "scan_1",
    ts: str = "2026-09-20T10:00:00.000+00:00",
    revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "ts_utc": ts,
        "trace_id": trace_id,
        "step_index": 1,
        "kind": "browser_telemetry",
        "outcome": TELEMETRY,
        "scenario": "B1",
        "operation": "browser.console",
        "actor": "synthetic-browser-monitor",
        "environment_kind": "baseline-light",
        "run_id": "test",
        "revision": REVISION if revision is None else revision,
        "input": {},
        "output": telemetry.telemetry_event(
            finding,
            page=PAGE,
            requests=[{"method": "POST", "path": "/api/v1/chart/data?x=1", "status": 200}],
            scan={"monitor": "b1", "scan_id": trace_id},
        ),
    }


def only(summary: telemetry.ScanSummary) -> telemetry.Finding:
    assert len(summary.findings) == 1, summary.as_dict()
    return summary.findings[0]


@pytest.fixture()
def store(tmp_path: Path) -> IncidentStore:
    return IncidentStore(
        tmp_path / "incidents.sqlite",
        target_repo=REPO,
        telemetry_admission=ENABLED,
    )


# ------------------------------------------------------------ qualification
def test_the_reproduced_warning_qualifies_with_its_real_severity() -> None:
    finding = only(telemetry.qualify([console()]))
    assert finding.classification == telemetry.QUALIFIED
    assert finding.severity == "warning", "the product logs a warning, not an error"
    assert finding.family == "bigint_number_format_not_applied"
    assert finding.stack, "the top frames are kept as diagnosis context"


def test_the_same_warning_at_another_severity_is_not_that_defect() -> None:
    """Severity is part of the symptom, so an 'error' is not this signature."""
    finding = only(telemetry.qualify([console(severity="error")]))
    assert finding.classification == telemetry.NEEDS_ATTENTION
    assert finding.family == ""


def test_a_rerender_burst_is_one_finding_with_a_count() -> None:
    summary = telemetry.qualify([console() for _ in range(8)])
    assert only(summary).count == 8


def test_an_unregistered_warning_is_visible_but_not_a_defect() -> None:
    finding = only(telemetry.qualify([console(text="Something else went wrong")]))
    assert finding.classification == telemetry.NEEDS_ATTENTION
    assert finding.reason


def test_deployment_noise_and_chatter_are_ignored() -> None:
    summary = telemetry.qualify(
        [
            console(text="A preload resource was preloaded using link preload but not used"),
            console(severity="log", text="rendering chart"),
        ]
    )
    assert [f.classification for f in summary.findings] == [
        telemetry.IGNORED,
        telemetry.IGNORED,
    ]


def test_malformed_and_oversized_input_is_refused_not_stored() -> None:
    summary = telemetry.qualify(
        ["not an object", {"severity": "warning"}, *[console() for _ in range(300)]]
    )
    assert summary.entries_seen == 302
    assert summary.entries_kept == telemetry.MAX_ENTRIES_PER_SCAN
    assert "entry is not an object" in summary.rejected


def test_credentials_and_query_state_never_reach_the_finding() -> None:
    finding = only(
        telemetry.qualify(
            [
                console(
                    text=WARNING + "\n    at fetch (password=hunter2 token=abc123)",
                    url="http://127.0.0.1:8588/static/assets/chunk.js?session=secret-cookie",
                )
            ]
        )
    )
    body = telemetry.telemetry_event(finding, page=PAGE)
    rendered = repr(body)
    assert "hunter2" not in rendered and "abc123" not in rendered
    assert "secret-cookie" not in rendered
    assert body["requests"] == []


def test_request_context_keeps_status_without_the_query_string() -> None:
    finding = only(telemetry.qualify([console()]))
    body = telemetry.telemetry_event(
        finding,
        page=PAGE,
        requests=[{"method": "POST", "path": "/api/v1/chart/data?form_data_key=abc", "status": 200}],
    )
    assert body["requests"] == [
        {"method": "POST", "path": "/api/v1/chart/data", "status": 200}
    ]


# --------------------------------------------------------------- admission
def test_a_qualified_finding_becomes_one_eligible_incident(store: IncidentStore) -> None:
    finding = only(telemetry.qualify([console()]))
    result = store.observe(telemetry_event(finding))
    assert result["action"] == "created"
    assert result["admission"] == ELIGIBLE
    incident = store.get(int(result["incident_id"]))
    assert incident is not None
    assert incident["scenario"] == "B1"
    assert incident["family"] == "bigint_number_format_not_applied"


def test_repeated_scans_of_the_same_defect_stay_one_incident(store: IncidentStore) -> None:
    finding = only(telemetry.qualify([console()]))
    store.observe(telemetry_event(finding, event_id="t1", trace_id="scan_1"))
    store.observe(telemetry_event(finding, event_id="t2", trace_id="scan_2"))
    assert store.totals()["incidents"] == 1
    assert store.totals()["failed_actions"] == 2, "each scan is one occurrence"


def test_the_same_event_delivered_twice_counts_once(store: IncidentStore) -> None:
    finding = only(telemetry.qualify([console()]))
    store.observe(telemetry_event(finding, event_id="t1"))
    again = store.observe(telemetry_event(finding, event_id="t1"))
    assert again["action"] == "duplicate_event"
    assert store.totals()["failed_actions"] == 1


def test_a_warning_free_scan_creates_nothing(store: IncidentStore) -> None:
    summary = telemetry.qualify([console(severity="log", text="4KiB rendered")])
    assert summary.qualified == []
    for index, finding in enumerate(summary.findings, start=1):
        store.observe(telemetry_event(finding, event_id=f"clean-{index}"))
    assert store.totals()["incidents"] == 0


def test_a_needs_attention_warning_is_recorded_but_never_dispatchable(
    store: IncidentStore,
) -> None:
    finding = only(telemetry.qualify([console(text="Unknown rendering problem")]))
    result = store.observe(telemetry_event(finding))
    assert result["action"] == "suppressed"
    assert result["reason"] == "needs_attention"
    assert store.totals()["incidents"] == 0
    assert store.processing_errors(), "an operator can still see it"


def test_dry_run_records_the_finding_and_blocks_dispatch(tmp_path: Path) -> None:
    store = IncidentStore(
        tmp_path / "dry.sqlite", target_repo=REPO, telemetry_admission=DRY_RUN
    )
    result = store.observe(telemetry_event(only(telemetry.qualify([console()]))))
    assert result["admission"] == BLOCKED
    assert store.eligible() == []


def test_disabled_admission_keeps_telemetry_out_entirely(tmp_path: Path) -> None:
    store = IncidentStore(
        tmp_path / "off.sqlite", target_repo=REPO, telemetry_admission=DISABLED
    )
    result = store.observe(telemetry_event(only(telemetry.qualify([console()]))))
    assert result == {"action": "suppressed", "reason": "telemetry_admission_disabled"}
    assert store.totals()["incidents"] == 0


def test_admission_is_rate_limited_per_hour(tmp_path: Path) -> None:
    store = IncidentStore(
        tmp_path / "rate.sqlite",
        target_repo=REPO,
        telemetry_admission=ENABLED,
        telemetry_rate_limit=2,
    )
    finding = only(telemetry.qualify([console()]))
    actions = [
        store.observe(
            telemetry_event(finding, event_id=f"r{i}", trace_id=f"scan_{i}")
        )["action"]
        for i in range(4)
    ]
    assert actions[:2] == ["created", "updated"]
    assert actions[2:] == ["suppressed", "suppressed"]


def test_evidence_that_cannot_be_pinned_to_the_baseline_is_blocked(
    store: IncidentStore,
) -> None:
    finding = only(telemetry.qualify([console()]))
    result = store.observe(
        telemetry_event(
            finding,
            revision={"strength": "unmeasured", "fixture_revision": "sha256:b1"},
        )
    )
    assert result["admission"] == BLOCKED
    assert store.eligible() == []


def test_a_forged_classification_cannot_admit_an_unregistered_message(
    store: IncidentStore,
) -> None:
    """The family must exist server-side; a body claiming one does not create it."""
    finding = only(telemetry.qualify([console(text="Unknown rendering problem")]))
    forged = telemetry_event(finding)
    forged["output"]["classification"] = telemetry.QUALIFIED
    forged["output"]["family"] = "not_a_registered_family"
    result = store.observe(forged)
    assert result == {"action": "suppressed", "reason": "unregistered_signature"}


def test_a_forged_severity_cannot_claim_the_registered_signature(
    store: IncidentStore,
) -> None:
    finding = only(telemetry.qualify([console()]))
    forged = telemetry_event(finding)
    forged["output"]["severity"] = "error"
    result = store.observe(forged)
    assert result == {"action": "suppressed", "reason": "severity_mismatch"}


def test_telemetry_survives_a_restart_through_the_event_log(tmp_path: Path) -> None:
    """A scan written while the folder was down is still folded afterwards."""
    events = EventStore(tmp_path / "events.sqlite", stream=open(tmp_path / "out.log", "w"))
    events.emit(telemetry_event(only(telemetry.qualify([console()]))))
    store = IncidentStore(
        tmp_path / "incidents.sqlite", target_repo=REPO, telemetry_admission=ENABLED
    )
    assert store.drain(events)["ingested"] == 1
    assert store.totals()["incidents"] == 1
    assert store.drain(events)["ingested"] == 0, "the cursor is durable"
