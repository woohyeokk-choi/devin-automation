"""The run summary: counts of stored rows, and what it refuses to claim."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from portal.controller import AWAITING_MERGE, MERGED, NEEDS_ATTENTION, TERMINAL
from portal.summary import summarise
from portal.verification import POST_MERGE, PREVIEW

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def repair(**values: Any) -> dict[str, Any]:
    return {
        "id": 1,
        "state": AWAITING_MERGE,
        "acu_limit": 20,
        "agent_acus": 0.0,
        "agent_pr_url": "https://github.com/woohyeokk-choi/superset/pull/6",
        "session_id": "e8b63e60",
        "deadline_utc": "2026-09-20T02:17:00+00:00",
        "updated_at": "2026-09-20T11:50:00+00:00",
    } | values


def attempt(**values: Any) -> dict[str, Any]:
    return {
        "repair_id": 1,
        "verdict": "passed",
        "stage": PREVIEW,
        "simulated": 0,
    } | values


def test_an_empty_run_counts_nothing_and_warns_about_nothing() -> None:
    summary = summarise(run="empty", incidents=[], repairs=[], attempts=[], now=NOW)

    assert (summary.incidents, summary.repairs, summary.preview_passed) == (0, 0, 0)
    assert summary.reported_acus == "unknown"
    assert summary.last_update_utc == ""
    assert summary.stale is False
    assert summary.notes == ()


def test_repeated_verification_of_one_repair_is_one_pass() -> None:
    summary = summarise(
        run="fresh",
        incidents=[{"id": 1}],
        repairs=[repair()],
        attempts=[
            attempt(verdict="blocked"),
            attempt(verdict="failed"),
            attempt(),
            attempt(),
        ],
        now=NOW,
    )

    assert summary.repairs == 1
    assert summary.preview_passed == 1
    assert summary.verification_attempts == 4
    assert summary.unsuccessful_verifications == 2
    assert summary.linked_prs == 1


def test_two_repairs_sharing_no_pull_request_are_counted_separately() -> None:
    summary = summarise(
        run="fresh",
        incidents=[{"id": 1}, {"id": 2}],
        repairs=[repair(), repair(id=2, agent_pr_url="", state=NEEDS_ATTENTION)],
        attempts=[attempt()],
        now=NOW,
    )

    assert summary.repairs == 2
    assert summary.linked_prs == 1
    assert summary.attention == 1
    assert dict(summary.states) == {AWAITING_MERGE: 1, NEEDS_ATTENTION: 1}


def test_a_preview_pass_is_never_counted_as_merge_verified() -> None:
    summary = summarise(
        run="fresh",
        incidents=[{"id": 1}],
        repairs=[repair()],
        attempts=[attempt()],
        now=NOW,
    )

    assert (summary.preview_passed, summary.merge_verified) == (1, 0)
    assert summary.awaiting_merge == 1


def test_a_post_merge_pass_is_counted_at_its_own_stage() -> None:
    summary = summarise(
        run="fresh",
        incidents=[{"id": 1}],
        repairs=[repair(state=MERGED)],
        attempts=[attempt(), attempt(stage=POST_MERGE)],
        now=NOW,
    )

    assert (summary.preview_passed, summary.merge_verified) == (1, 1)


def test_a_simulated_pass_is_not_evidence() -> None:
    summary = summarise(
        run="sim",
        incidents=[{"id": 1}],
        repairs=[repair()],
        attempts=[attempt(simulated=1)],
        now=NOW,
    )

    assert summary.preview_passed == 0


def test_an_old_run_with_open_work_is_reported_as_unpolled_not_as_stopped() -> None:
    summary = summarise(
        run="fresh",
        incidents=[{"id": 1}],
        repairs=[repair(updated_at="2026-09-20T02:08:18+00:00")],
        attempts=[attempt()],
        now=NOW,
    )

    assert summary.stale is True
    assert summary.quiet_for == "9h 51m"
    assert summary.open_work == 1
    joined = " ".join(summary.notes)
    assert "last stored snapshot" in joined
    assert "coordinator" not in joined.lower() or "not something this page can see" in joined
    assert "stopped" not in joined


def test_a_settled_run_is_quiet_rather_than_stale() -> None:
    summary = summarise(
        run="old",
        incidents=[{"id": 1}],
        repairs=[repair(state=TERMINAL, updated_at="2026-09-18T02:08:18+00:00")],
        attempts=[],
        now=NOW,
    )

    assert summary.stale is False
    assert summary.open_work == 0
    assert summary.deadlines_elapsed == 0


def test_an_elapsed_deadline_is_reported_without_being_extended() -> None:
    summary = summarise(
        run="fresh",
        incidents=[{"id": 1}],
        repairs=[repair()],
        attempts=[attempt()],
        now=NOW,
    )

    assert summary.deadlines_elapsed == 1
    assert any("past the deadline" in note for note in summary.notes)


def test_unreported_consumption_reads_as_unknown_and_never_as_free() -> None:
    summary = summarise(
        run="fresh",
        incidents=[{"id": 1}],
        repairs=[repair()],
        attempts=[attempt()],
        now=NOW,
    )

    assert summary.requested_acus == 20
    assert summary.reported_acus == "unknown"
    assert any("not evidence that the work was free" in note for note in summary.notes)

    reported = summarise(
        run="fresh",
        incidents=[{"id": 1}],
        repairs=[repair(agent_acus=3.5)],
        attempts=[attempt()],
        now=NOW,
    )
    assert reported.reported_acus == "3.5"
    assert not any("free" in note for note in reported.notes)


def test_failed_and_unknown_notifications_are_surfaced() -> None:
    summary = summarise(
        run="fresh",
        incidents=[{"id": 1}],
        repairs=[repair()],
        attempts=[attempt()],
        processing_errors=[{"reason": "unknown family"}],
        notification_totals={"sent": 3, "failed": 1, "unknown": 2, "disabled": 5},
        now=NOW,
    )

    assert summary.undelivered_notifications == 3
    assert summary.processing_errors == 1


def test_the_console_renders_the_run_summary_and_unknown_consumption() -> None:
    """The page a reviewer opens, not just the function behind it."""
    from fastapi.testclient import TestClient

    from portal import app as portal_app
    from portal.config import settings
    from test_controller import event

    portal_app.store.emit(event(event_id="summary-1", trace_id="summary-trace"))
    incident = next(i for i in portal_app.incidents.list() if i["scenario"] == "S2")
    stored = portal_app.repairs.by_fingerprint(str(incident["fingerprint"]))
    assert stored is not None
    portal_app.repairs.update(
        int(stored["id"]),
        state=AWAITING_MERGE,
        agent_status="running",
        agent_detail="waiting_for_user",
        agent_acus=0.0,
        agent_pr_url="https://github.com/acme/superset/pull/8",
    )

    auth = (settings.ops_username, settings.ops_password)
    client = TestClient(portal_app.app)
    listing = client.get("/ops/incidents", auth=auth).text
    detail = client.get(f"/ops/incidents/{incident['id']}", auth=auth).text

    assert "This run" in listing
    assert "Independent preview passes" in listing
    assert "Merge-verified" in listing
    assert "Last recorded change" in listing
    # Reported nothing is unknown on both pages, and never rendered as zero cost.
    assert "consumption unknown" in detail
    assert "ACU requested" in detail


def test_a_malformed_timestamp_does_not_invent_freshness() -> None:
    summary = summarise(
        run="fresh",
        incidents=[{"id": 1}],
        repairs=[repair(updated_at="not a timestamp", deadline_utc="")],
        attempts=[],
        now=NOW,
        stale_after=timedelta(minutes=1),
    )

    assert summary.last_update_utc == ""
    assert summary.quiet_for == ""
    assert summary.stale is False
