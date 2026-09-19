"""Incident model: what counts, what dedups and what is never an incident."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from portal.events import EventStore
from portal.handoff import FILES, sha256_of, write_bundle
from portal.incidents import BLOCKED, ELIGIBLE, IncidentStore, family_for, fingerprint

REPO = "woohyeokk-choi/superset"
BASELINE = "394bca55c792b7b3547e23f6e175a7cb0f0757e8"
REVISION = {
    "checkout_sha": BASELINE,
    "checkout_matches_running_code": True,
    "strength": "measured",
    "fixture_revision": "sha256:67ff039835f9890c",
}


def event(
    *,
    event_id: str,
    trace_id: str = "trace_1",
    assertion: str | None = "new_exploration_does_not_reuse_a_discarded_key",
    outcome: str = "assertion_failed",
    actor: str = "analyst",
    environment_kind: str = "baseline-light",
    step: int = 1,
    holds: bool = False,
    subject: str = "product_contract",
    scenario: str | None = None,
    revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    family = family_for(assertion or "")
    return {
        "event_id": event_id,
        "ts_utc": "2026-09-19T12:00:00.000+00:00",
        "trace_id": trace_id,
        "step_index": step,
        "kind": "assertion" if assertion else "user_action",
        "outcome": outcome,
        "scenario": scenario or (family.scenario if family else "S2"),
        "operation": "portal.save_exploration",
        "actor": actor,
        "environment_kind": environment_kind,
        "run_id": "test",
        "http_status": 200,
        "revision": REVISION if revision is None else revision,
        "input": {},
        "output": {},
        "assertion": (
            {
                "name": assertion,
                "expected": "a fresh key",
                "observed": "the discarded key",
                "holds": holds,
                "subject": subject,
                "known_baseline_defect": True,
            }
            if assertion
            else None
        ),
    }


@pytest.fixture()
def store(tmp_path: Path) -> IncidentStore:
    return IncidentStore(tmp_path / "incidents.sqlite", target_repo=REPO)


def test_a_failed_action_creates_one_incident(store: IncidentStore) -> None:
    result = store.observe(event(event_id="e1"))
    assert result["action"] == "created"
    assert result["new_occurrence"] is True
    assert store.totals() == {
        "incidents": 1,
        "failed_actions": 1,
        "events": 1,
        "processing_errors": 0,
    }


def test_replaying_the_same_event_changes_no_count(store: IncidentStore) -> None:
    store.observe(event(event_id="e1"))
    again = store.observe(event(event_id="e1"))
    assert again["action"] == "duplicate_event"
    assert store.totals()["failed_actions"] == 1
    assert store.totals()["events"] == 1


def test_sibling_assertions_in_one_action_are_one_occurrence(store: IncidentStore) -> None:
    store.observe(event(event_id="e1"))
    store.observe(
        event(
            event_id="e2",
            step=2,
            assertion="discarded_exploration_link_stays_gone",
        )
    )
    totals = store.totals()
    assert (totals["incidents"], totals["failed_actions"], totals["events"]) == (1, 1, 2)


def test_a_new_failed_action_increments_the_occurrence_once(store: IncidentStore) -> None:
    store.observe(event(event_id="e1", trace_id="trace_1"))
    store.observe(event(event_id="e2", trace_id="trace_2"))
    totals = store.totals()
    assert (totals["incidents"], totals["failed_actions"]) == (1, 2)


def test_s1_is_a_separate_incident(store: IncidentStore) -> None:
    store.observe(event(event_id="e1"))
    store.observe(
        event(
            event_id="e2",
            trace_id="trace_2",
            assertion="row_limit_survives_an_unrelated_change",
        )
    )
    families = {item["family"] for item in store.list()}
    assert families == {"discarded_form_data_key_is_reused", "omitted_row_limit_is_reset"}


def test_a_different_profile_is_a_different_incident(store: IncidentStore) -> None:
    store.observe(event(event_id="e1"))
    store.observe(event(event_id="e2", trace_id="trace_2", actor="restricted_viewer"))
    assert store.totals()["incidents"] == 2


def test_the_fingerprint_ignores_transient_detail() -> None:
    a = fingerprint(REPO, BASELINE, "discarded_form_data_key_is_reused", "analyst")
    b = fingerprint(REPO, BASELINE, "discarded_form_data_key_is_reused", "analyst")
    assert a == b
    assert a != fingerprint(REPO, "other-sha", "discarded_form_data_key_is_reused", "analyst")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"outcome": "expected_denial", "assertion": None},  # N1 control
        {"outcome": "blocked", "assertion": None},  # setup failure
        {"outcome": "error", "assertion": None},
        {"outcome": "ok", "holds": True},  # the contract held
        {"assertion": "some_unregistered_check"},  # unknown scenario
        {"subject": "harness"},  # the portal's own plumbing
    ],
)
def test_non_product_failures_create_no_incident(
    store: IncidentStore, kwargs: dict[str, Any]
) -> None:
    result = store.observe(event(event_id="e1", **kwargs))
    assert result["action"] == "suppressed"
    assert store.totals()["incidents"] == 0


def test_two_denial_events_from_one_attempt_are_not_two_incidents(
    store: IncidentStore,
) -> None:
    store.observe(event(event_id="e1", outcome="expected_denial", assertion=None))
    store.observe(
        event(event_id="e2", step=2, outcome="expected_denial", assertion=None)
    )
    assert store.totals()["incidents"] == 0


def test_a_failing_control_does_not_qualify_its_family(store: IncidentStore) -> None:
    # Colour preservation holds at this baseline; if it ever breaks that is new
    # behaviour to reproduce, not an S1 repair case.
    result = store.observe(
        event(event_id="e1", assertion="color_scheme_survives_an_unrelated_change")
    )
    assert result["reason"] == "control_assertion"
    assert store.totals()["incidents"] == 0


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"environment_kind": "unexpected-env"}, "unknown_environment"),
        ({"environment_kind": ""}, "unknown_environment"),
        ({"actor": ""}, "missing_actor"),
    ],
)
def test_admission_fails_closed_on_untrusted_context(
    store: IncidentStore, kwargs: dict[str, Any], reason: str
) -> None:
    result = store.observe(event(event_id="e1", **kwargs))
    assert (result["action"], result["reason"]) == ("suppressed", reason)
    assert store.totals()["incidents"] == 0
    assert store.processing_errors()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"revision": {"strength": "unmeasured"}},  # provenance unprovable
        {"revision": {**REVISION, "fixture_revision": None}},  # unknown fixture
        {"scenario": "S1"},  # scenario disagrees with the family
    ],
)
def test_unprovable_evidence_is_recorded_but_never_dispatchable(
    store: IncidentStore, kwargs: dict[str, Any]
) -> None:
    result = store.observe(event(event_id="e1", **kwargs))
    assert result["admission"] == BLOCKED
    assert store.list()[0]["admission_reason"]
    assert store.eligible() == []


def test_a_matching_baseline_is_dispatchable_and_a_mismatch_is_not(tmp_path: Path) -> None:
    store = IncidentStore(
        tmp_path / "i.sqlite", target_repo=REPO, expected_baseline=BASELINE
    )
    assert store.observe(event(event_id="e1"))["admission"] == ELIGIBLE
    assert len(store.eligible()) == 1

    other = IncidentStore(
        tmp_path / "j.sqlite", target_repo=REPO, expected_baseline="deadbeef" * 5
    )
    assert other.observe(event(event_id="e2"))["admission"] == BLOCKED
    assert other.eligible() == []


def test_a_preview_event_cannot_create_an_incident(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path / "i.sqlite", target_repo=REPO)
    result = store.observe(event(event_id="e1", environment_kind="preview"))
    assert result["action"] == "suppressed"
    assert store.totals()["incidents"] == 0
    assert store.processing_errors()[0]["reason"].startswith("preview event")


def test_a_preview_event_attaches_to_its_configured_parent(tmp_path: Path) -> None:
    baseline = IncidentStore(tmp_path / "i.sqlite", target_repo=REPO)
    parent = baseline.observe(event(event_id="e1"))["fingerprint"]

    preview = IncidentStore(
        tmp_path / "i.sqlite", target_repo=REPO, parent_fingerprint=parent
    )
    result = preview.observe(
        event(event_id="e2", trace_id="trace_preview", environment_kind="reproduction")
    )
    assert result["action"] == "updated"
    assert result["derived"] is True
    totals = preview.totals()
    # Evidence grew; the failed-action count did not, so a repair session's own
    # reproduction cannot look like the bug happening again in production.
    assert (totals["incidents"], totals["failed_actions"], totals["events"]) == (1, 1, 2)


def test_a_preview_event_with_a_nonexistent_parent_is_suppressed(tmp_path: Path) -> None:
    # Regression: this path records a processing error from inside the open
    # transaction, which deadlocked against a non-reentrant lock.
    store = IncidentStore(
        tmp_path / "i.sqlite", target_repo=REPO, parent_fingerprint="nonexistent-parent"
    )
    done = threading.Event()
    outcome: list[dict[str, Any]] = []

    def run() -> None:
        outcome.append(store.observe(event(event_id="e1", environment_kind="preview")))
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(timeout=5), "observe() blocked on its own lock"
    assert outcome[0]["reason"] == "unknown_parent"
    assert store.totals()["incidents"] == 0
    assert store.processing_errors()[0]["reason"] == "parent incident does not exist"


# ----------------------------------------------------------------- catch-up
def test_the_drain_recovers_events_the_observer_never_saw(tmp_path: Path) -> None:
    events = EventStore(tmp_path / "events.sqlite", stream=open(tmp_path / "out", "w"))
    store = IncidentStore(tmp_path / "incidents.sqlite", target_repo=REPO)

    # The crash window: committed to the event log, never handed to the engine.
    events.emit(event(event_id="e1"))
    events.emit(event(event_id="e2", trace_id="trace_2"))
    assert store.totals()["incidents"] == 0

    assert store.drain(events) == {"scanned": 2, "ingested": 2}
    assert store.totals()["failed_actions"] == 2
    # Idempotent: a second drain sees nothing new, and even a rescan from zero
    # cannot double-count.
    assert store.drain(events)["scanned"] == 0
    store._set_cursor(0)
    store.drain(events)
    assert store.totals()["failed_actions"] == 2


def test_the_drain_cursor_survives_a_restart(tmp_path: Path) -> None:
    events = EventStore(tmp_path / "events.sqlite", stream=open(tmp_path / "out", "w"))
    path = tmp_path / "incidents.sqlite"
    first = IncidentStore(path, target_repo=REPO)
    events.emit(event(event_id="e1"))
    first.drain(events)

    events.emit(event(event_id="e2", trace_id="trace_2"))
    reopened = IncidentStore(path, target_repo=REPO)
    assert reopened.drain(events) == {"scanned": 1, "ingested": 1}
    assert reopened.totals()["failed_actions"] == 2


def test_the_live_observer_and_the_drain_do_not_double_count(tmp_path: Path) -> None:
    events = EventStore(tmp_path / "events.sqlite", stream=open(tmp_path / "out", "w"))
    store = IncidentStore(tmp_path / "incidents.sqlite", target_repo=REPO)
    events.observer = store.observe
    events.emit(event(event_id="e1"))
    store.drain(events)
    assert store.totals() == {
        "incidents": 1,
        "failed_actions": 1,
        "events": 1,
        "processing_errors": 0,
    }


def test_an_unmeasured_baseline_is_not_merged_with_a_verified_one(
    store: IncidentStore,
) -> None:
    store.observe(event(event_id="e1"))
    unmeasured = event(event_id="e2", trace_id="trace_2")
    unmeasured["revision"] = {"strength": "unmeasured"}
    store.observe(unmeasured)
    assert {i["baseline_sha"] for i in store.list()} == {BASELINE, "unverified"}


def test_an_event_without_an_id_is_a_processing_error(store: IncidentStore) -> None:
    broken = event(event_id="e1")
    del broken["event_id"]
    assert store.observe(broken)["action"] == "processing_error"
    assert store.totals()["incidents"] == 0


def test_concurrent_duplicate_delivery_counts_once(store: IncidentStore) -> None:
    results: list[dict[str, Any]] = []
    threads = [
        threading.Thread(target=lambda: results.append(store.observe(event(event_id="e1"))))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(r["action"] == "duplicate_event" for r in results) == 7
    totals = store.totals()
    assert (totals["incidents"], totals["failed_actions"], totals["events"]) == (1, 1, 1)


def test_counts_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "incidents.sqlite"
    first = IncidentStore(path, target_repo=REPO)
    first.observe(event(event_id="e1"))
    first.observe(event(event_id="e2", trace_id="trace_2"))

    reopened = IncidentStore(path, target_repo=REPO)
    assert reopened.totals()["failed_actions"] == 2
    # And a replay of an event from before the restart still does not inflate.
    assert reopened.observe(event(event_id="e1"))["action"] == "duplicate_event"
    assert reopened.totals()["failed_actions"] == 2


# ------------------------------------------------------------------ bundle
def test_the_bundle_is_parseable_and_checksummed(store: IncidentStore, tmp_path: Path) -> None:
    store.observe(event(event_id="e1"))
    store.observe(
        event(event_id="e2", step=2, assertion="discarded_exploration_link_stays_gone")
    )
    incident = store.get(1)
    assert incident is not None

    out = tmp_path / "bundle"
    manifest = write_bundle(incident, out, versions={"portal_run_id": "test"})

    assert sorted(p.name for p in out.iterdir()) == sorted(FILES)
    json.loads((out / "incident.json").read_text())
    events = [json.loads(line) for line in (out / "events.redacted.jsonl").read_text().splitlines()]
    assert len(events) == 2
    for entry in manifest["files"]:
        assert entry["sha256"] == sha256_of(out / entry["name"])

    reproduction = (out / "reproduction.md").read_text()
    assert BASELINE in reproduction and REPO in reproduction
    assert "same workspace for the whole scenario" in reproduction  # exact-session step
    assert "docker compose" in reproduction


def test_the_bundle_carries_whole_traces_not_only_assertions(tmp_path: Path) -> None:
    events = EventStore(tmp_path / "events.sqlite", stream=open(tmp_path / "out", "w"))
    store = IncidentStore(tmp_path / "incidents.sqlite", target_repo=REPO)
    store.event_log = events
    events.observer = store.observe

    # The request that set the failure up, then the failing assertion.
    request = event(event_id="e0", assertion=None, outcome="ok")
    request["operation"] = "superset.delete_form_data"
    events.emit(request)
    events.emit(event(event_id="e1", step=2))

    incident = store.get(1)
    assert incident is not None
    out = tmp_path / "bundle"
    manifest = write_bundle(
        incident, out, versions={"automation_sha": "abc123", "automation_dirty": False}
    )

    exported = [
        json.loads(line)
        for line in (out / "events.redacted.jsonl").read_text().splitlines()
    ]
    assert [e["operation"] for e in exported] == [
        "superset.delete_form_data",
        "portal.save_exploration",
    ]
    assert manifest["automation_sha"] == "abc123"
    assert manifest["evidence_gaps"] == []
    reproduction = (out / "reproduction.md").read_text()
    assert "git checkout abc123" in reproduction
    assert "complete stored trace" in reproduction


def test_the_bundle_states_the_gap_when_a_trace_is_gone(store: IncidentStore, tmp_path: Path) -> None:
    # No event log attached: only the assertions survive, and the bundle has to
    # say so rather than present them as the request sequence.
    store.observe(event(event_id="e1"))
    incident = store.get(1)
    assert incident is not None
    manifest = write_bundle(incident, tmp_path / "bundle", versions={})
    assert manifest["evidence_gaps"]
    reproduction = (tmp_path / "bundle" / "reproduction.md").read_text()
    assert "no longer in the event log" in reproduction
    assert "WARNING: the automation commit was not measured" in reproduction


def test_the_bundle_carries_no_credentials(store: IncidentStore, tmp_path: Path) -> None:
    leaky = event(event_id="e1")
    leaky["input"] = {"authorization": "Bearer canary-credential-12345"}
    store.observe(leaky)
    incident = store.get(1)
    assert incident is not None
    write_bundle(incident, tmp_path / "bundle", versions={})
    for name in FILES:
        assert "canary-credential-12345" not in (tmp_path / "bundle" / name).read_text()
