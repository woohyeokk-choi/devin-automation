"""The two-process deployment: portal container, host coordinator, one directory.

Nothing here touches the network. What is under test is the wiring itself —
the portal writing events and proposals into the shared state directory, and
`portal.coordinator` opening that same state and dispatching from it — because
the two halves are only a deployment if the second one can read the first
one's queue and its evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from portal.config import Settings
from portal.controller import DISPATCHED, PROPOSED, RepairStore
from portal.coordinator import build_worker, open_state, single_instance
from portal.events import EventStore
from portal.incidents import IncidentStore
from portal.providers import Devin, GitHub
from portal.simulation import FakeDevin, FakeGitHub, FakeTransport
from portal.worker import build_controller

from test_incidents import BASELINE, REPO, REVISION, event

#: The one thing the portal knows about the upstream request that failed, and
#: the one thing an assertion-only handoff loses.
CALL = "superset.save_exploration"


def deployment(state_dir: Path, *, dispatch: bool) -> Settings:
    """Both processes' configuration: identical but for who may dispatch."""
    return Settings(
        data_dir=state_dir,
        target_repo=REPO,
        baseline_sha=BASELINE,
        auto_repair_enabled=dispatch,
        automation_dir=str(state_dir / "automation"),
        verification_workspace=str(state_dir / "work"),
        verification_artifacts=str(state_dir / "artifacts"),
    )


def upstream_call(trace_id: str) -> dict[str, Any]:
    """The request the portal made just before the assertion failed."""
    return {
        "event_id": "call-1",
        "ts_utc": "2026-09-19T11:59:59.000+00:00",
        "trace_id": trace_id,
        "step_index": 1,
        "kind": "upstream_call",
        "outcome": "ok",
        "operation": CALL,
        "actor": "analyst",
        "environment_kind": "baseline-light",
        "run_id": "test",
        "http_status": 201,
        "input": {"datasource_id": 22, "row_limit": 137},
        "output": {"key": "kEy1", "reused": True},
        "revision": REVISION,
    }


def portal_side(config: Settings) -> tuple[EventStore, IncidentStore]:
    """`portal.app`'s wiring, minus the HTTP layer.

    The container observes and proposes with dispatch off: no provider is
    built, no credential is read, and the proposal is a row in the shared
    database rather than a call.
    """
    events = EventStore(config.db_path)
    incidents = IncidentStore(
        config.db_path.with_name("incidents.sqlite"),
        target_repo=config.target_repo,
        parent_fingerprint=config.parent_incident,
        expected_baseline=config.baseline_sha,
    )
    incidents.event_log = events
    controller = build_controller(
        RepairStore(config.db_path.with_name("repairs.sqlite")),
        target_repo=config.target_repo,
        versions={"automation_sha": "abc123"},
        dispatch_enabled=False,
        incident_of=incidents.get,
    )

    def observe(record: dict[str, Any]) -> dict[str, Any]:
        result = incidents.observe(record)
        found = incidents.by_fingerprint(str(result.get("fingerprint") or ""))
        if found is not None:
            controller.consider(found)
        return result

    events.observer = observe
    return events, incidents


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


def fake_providers() -> tuple[tuple[GitHub, Devin], FakeDevin, FakeTransport]:
    """Injected clients over a scripted wire, so nothing leaves the process."""
    devin_api = FakeDevin()
    devin_wire = FakeTransport(devin_api)
    return (
        (
            GitHub(FakeTransport(FakeGitHub()), token="simulated-token", repo=REPO),
            Devin(devin_wire, api_key="cog_simulated", org_id="org-simulated"),
        ),
        devin_api,
        devin_wire,
    )


def test_the_coordinator_reads_the_portal_queue_with_the_full_trace(
    state_dir: Path,
) -> None:
    """A portal event becomes a dispatch that carries the request around it."""
    events, _incidents = portal_side(deployment(state_dir, dispatch=False))
    events.emit(upstream_call("trace-deploy"))
    events.emit(event(event_id="assert-1", trace_id="trace-deploy", step=2))

    providers, devin_api, devin_wire = fake_providers()
    worker, state = build_worker(
        deployment(state_dir, dispatch=True), providers=providers
    )
    decisions = [d for d in worker.tick() if d.action != "deferred"]

    assert [d.action for d in decisions] == ["dispatched"]
    assert [r["state"] for r in state.repairs.list()] == [DISPATCHED]
    assert len(devin_api.sessions) == 1
    created = [c for c in devin_wire.calls if c.method == "POST"]
    prompt = (created[0].json or {})["prompt"]
    assert CALL in prompt, "the coordinator dispatched assertions with no request trace"
    assert '"http_status": 201' in prompt
    assert "no longer in the event log" not in prompt
    assert "cog_simulated" not in prompt and "password" not in prompt.lower()


def test_reconstruction_without_the_event_log_would_lose_the_trace(
    state_dir: Path,
) -> None:
    """Why `open_state` attaches it: the incident alone holds no requests.

    This is the defect the wiring test above guards against, stated directly —
    an `IncidentStore` over the same file, without its event log, reports the
    same failing assertions and no trace events at all.
    """
    events, _incidents = portal_side(deployment(state_dir, dispatch=False))
    events.emit(upstream_call("trace-deploy"))
    events.emit(event(event_id="assert-1", trace_id="trace-deploy", step=2))
    config = deployment(state_dir, dispatch=True)

    detached = IncidentStore(
        config.db_path.with_name("incidents.sqlite"), target_repo=REPO
    )
    blind = detached.get(1) or {}
    assert blind["events"], "the incident itself is there"
    assert blind["trace_events"] == {}

    attached = open_state(config).incidents.get(1) or {}
    assert attached["trace_events"]["trace-deploy"], "the coordinator sees the requests"
    assert [e["operation"] for e in attached["trace_events"]["trace-deploy"]] == [
        CALL,
        "portal.save_exploration",
    ]


def test_the_coordinator_opens_the_same_files_the_portal_wrote(
    state_dir: Path,
) -> None:
    """One directory, one queue. Two directories would be two queues."""
    events, _incidents = portal_side(deployment(state_dir, dispatch=False))
    events.emit(event(event_id="assert-1"))
    config = deployment(state_dir, dispatch=True)

    state = open_state(config)
    assert [r["state"] for r in state.repairs.list()] == [PROPOSED]
    assert state.incidents.list(), "the portal's incident is the coordinator's"
    assert sorted(p.name for p in state_dir.glob("*.sqlite")) == [
        "events.sqlite",
        "incidents.sqlite",
        "repairs.sqlite",
        "verifications.sqlite",
    ]


def test_a_coordinator_pointed_elsewhere_finds_nothing(
    state_dir: Path, tmp_path: Path
) -> None:
    """The failure mode the compose bind mount exists to prevent."""
    events, _incidents = portal_side(deployment(state_dir, dispatch=False))
    events.emit(event(event_id="assert-1"))

    elsewhere = open_state(deployment(tmp_path / "other", dispatch=True))
    assert elsewhere.repairs.list() == []
    assert elsewhere.incidents.list() == []


def test_a_second_coordinator_over_one_state_directory_is_refused(
    state_dir: Path,
) -> None:
    """Two loops would delete each other's candidate checkout mid-verification.

    The repair slot in SQLite does not cover this: a verification is a
    checkout and a Compose project named after the candidate commit, which
    `IsolatedStack.prepare` removes and recreates.
    """
    with single_instance(state_dir):
        with pytest.raises(SystemExit) as refused:
            with single_instance(state_dir):
                pass

    assert str(state_dir) in str(refused.value)


def test_the_lock_is_free_once_the_coordinator_leaves(state_dir: Path) -> None:
    with single_instance(state_dir):
        pass

    with single_instance(state_dir):
        pass
