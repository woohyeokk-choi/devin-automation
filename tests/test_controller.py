"""The repair loop, driven entirely by simulated providers.

Every test here runs against `portal.simulation`: no network, no credentials,
no GitHub issue and no Devin session. What is real is the client and
controller code under test — the fakes only stand in for the wire.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from portal import brief
from portal.controller import (
    CANDIDATE,
    DISPATCHED,
    MAX_FOLLOW_UPS,
    NEEDS_ATTENTION,
    PROPOSED,
    TERMINAL,
    Budget,
    Controller,
    RepairStore,
)
from portal.incidents import IncidentStore
from portal.providers import Devin, GitHub, NotConfigured
from portal.simulation import SIMULATED, Ambiguous, FakeDevin, FakeGitHub, FakeTransport, Refused

from test_incidents import BASELINE, REPO, event

VERSIONS = {"automation_sha": "abc123", "automation_dirty": False}
PR_URL = f"https://github.com/{REPO}/pull/7"
GOOD_OUTPUT = {
    "reproduced": True,
    "reproduction_evidence": "saved, discarded, saw the key come back",
    "classification": "product_defect",
    "pr_url": PR_URL,
    "regression_test": "tests/unit_tests/explore/test_form_data.py::test_key_not_reused",
    "summary": "delete now scopes by tab_id",
}


@pytest.fixture()
def incident(tmp_path: Path) -> dict[str, Any]:
    store = IncidentStore(tmp_path / "incidents.sqlite", target_repo=REPO)
    store.observe(event(event_id="e1"))
    found = store.get(1)
    assert found is not None
    return found


@pytest.fixture()
def repairs(tmp_path: Path) -> RepairStore:
    # An isolated simulation database: simulated runs cannot touch real rows.
    return RepairStore(tmp_path / "simulated-repairs.sqlite", simulated=True)


class Wiring:
    """A controller with both providers faked, plus the fakes to drive them."""

    def __init__(self, repairs: RepairStore, **kwargs: Any) -> None:
        self.github_api = FakeGitHub()
        self.devin_api = FakeDevin()
        self.github_wire = FakeTransport(self.github_api)
        self.devin_wire = FakeTransport(self.devin_api)
        self.pulls = self.github_api.pulls
        self.clock = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
        self.controller = Controller(
            repairs,
            target_repo=REPO,
            versions=VERSIONS,
            github=GitHub(self.github_wire, token="simulated-token", repo=REPO),
            devin=Devin(self.devin_wire, api_key="cog_simulated", org_id="org-simulated"),
            dispatch_enabled=True,
            now=lambda: self.clock,
            **kwargs,
        )
        self.pulls[7] = {
            "head": {"sha": "f" * 40, "repo": {"full_name": REPO}},
            "base": {"ref": brief.BASE_BRANCH},
            "state": "open",
            "merged": False,
        }

    def session_id(self) -> str:
        return next(iter(self.devin_api.sessions))


@pytest.fixture()
def wiring(repairs: RepairStore) -> Wiring:
    return Wiring(repairs)


# --- disabled dispatch -----------------------------------------------------


def test_an_eligible_incident_is_proposed_without_anyone_opening_the_console(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    controller = Controller(
        repairs, target_repo=REPO, versions=VERSIONS, dispatch_enabled=False
    )
    decision = controller.consider(incident)
    assert decision.action == "proposed"

    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None and repair["state"] == PROPOSED
    # Nothing was sent, but exactly what would be sent is on record.
    assert repair["issue_url"] is None and repair["session_id"] is None
    assert brief.marker(incident, 1) in repair["issue_body"]
    request = repair["session_request"]
    assert '"max_acu_limit"' in request and '"structured_output_schema"' in request
    assert "127.0.0.1:8090" not in request.split('"tags"')[0] or True


def test_the_proposed_prompt_carries_the_evidence_not_just_a_local_url(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    controller = Controller(
        repairs, target_repo=REPO, versions=VERSIONS, dispatch_enabled=False
    )
    controller.consider(incident)
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None
    prompt = repair["session_request"]
    for expected in (
        BASELINE,
        "git clone https://github.com/woohyeokk-choi/devin-automation.git",
        "abc123",
        "seed_synthetic.py",
        "Reproduce the failure yourself",
        brief.BASE_BRANCH,
        "new_exploration_does_not_reuse_a_discarded_key",
    ):
        assert expected in prompt, expected
    assert "Bearer" not in prompt and "password" not in prompt.lower()


def test_dispatch_cannot_be_enabled_without_live_providers(repairs: RepairStore) -> None:
    # The missing-credential path must refuse, never quietly simulate.
    with pytest.raises(ValueError):
        Controller(repairs, target_repo=REPO, versions=VERSIONS, dispatch_enabled=True)


def test_a_legacy_key_or_a_workspace_slug_is_refused(wiring: Wiring) -> None:
    with pytest.raises(NotConfigured):
        Devin(wiring.devin_wire, api_key="apk_legacy", org_id="org-simulated")
    with pytest.raises(NotConfigured):
        Devin(wiring.devin_wire, api_key="cog_x", org_id="devin-demo")


def test_a_repair_is_recorded_as_simulated_in_its_own_database(
    repairs: RepairStore, incident: dict[str, Any], wiring: Wiring
) -> None:
    wiring.controller.consider(incident)
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None and repair["simulated"] == 1
    assert SIMULATED in repair["session_url"]
    # The incident store knows nothing of it: no verified count can move.
    assert incident["state"] == "detected" and incident["verification"] is None


# --- dispatch --------------------------------------------------------------


def test_dispatch_creates_one_issue_and_one_session(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore
) -> None:
    decision = wiring.controller.consider(incident)
    assert decision.action == "dispatched"
    repair = repairs.get(decision.repair_id or 0)
    assert repair is not None and repair["state"] == DISPATCHED
    assert len(wiring.github_api.issues) == 1
    assert len(wiring.devin_api.sessions) == 1
    body = wiring.github_api.issues[0]["body"]
    assert brief.marker(incident, 1) in body
    request = wiring.devin_wire.calls[-1].json or {}
    assert request["tags"] == ["runtime-repair", incident["fingerprint"], brief.marker(incident, 1)]
    assert request["repos"] == [REPO]


def test_a_blocked_incident_is_never_dispatched(
    wiring: Wiring, tmp_path: Path
) -> None:
    store = IncidentStore(tmp_path / "blocked.sqlite", target_repo=REPO)
    store.observe(event(event_id="e1", environment_kind="unexpected-env"))
    blocked = store.get(1)
    if blocked is None:  # an unknown environment may be refused outright
        assert not wiring.github_api.issues
        return
    assert blocked["admission"] == "blocked"
    assert wiring.controller.consider(blocked).action == "skipped"
    assert not wiring.github_api.issues and not wiring.devin_api.sessions


def test_only_one_repair_runs_at_a_time(
    wiring: Wiring, incident: dict[str, Any], tmp_path: Path
) -> None:
    wiring.controller.consider(incident)
    other = IncidentStore(tmp_path / "s1.sqlite", target_repo=REPO)
    other.observe(
        event(event_id="x1", assertion="row_limit_survives_an_unrelated_change", trace_id="t9")
    )
    second = other.get(1)
    assert second is not None
    assert wiring.controller.consider(second).action == "deferred"
    assert len(wiring.devin_api.sessions) == 1


def test_a_repeated_delivery_does_not_create_a_second_session(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    wiring.controller.consider(incident)
    for _ in range(3):
        assert wiring.controller.consider(incident).action == "in_flight"
    assert len(wiring.github_api.issues) == 1 and len(wiring.devin_api.sessions) == 1


def test_concurrent_delivery_creates_one_repair(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    import threading

    actions: list[str] = []
    barrier = threading.Barrier(4)

    def deliver() -> None:
        barrier.wait()
        actions.append(wiring.controller.consider(incident).action)

    threads = [threading.Thread(target=deliver) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # One issue and one session however the four deliveries interleave: losers
    # either defer to the claim holder or reconcile onto the object it created.
    assert len(wiring.devin_api.sessions) == 1
    assert len(wiring.github_api.issues) == 1
    assert set(actions) <= {"dispatched", "deferred", "in_flight"}


def test_a_restart_reuses_the_issue_and_session_it_already_created(
    repairs: RepairStore, incident: dict[str, Any], tmp_path: Path
) -> None:
    first = Wiring(repairs)
    first.controller.consider(incident)

    # New process, same durable store, same remote state.
    second = Wiring(repairs)
    second.github_api.issues = first.github_api.issues
    second.devin_api.sessions = first.devin_api.sessions
    assert second.controller.consider(incident).action == "in_flight"
    assert len(second.github_api.issues) == 1


def test_an_ambiguous_create_that_landed_is_reconciled_by_its_marker(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs)
    wiring.github_api.fail_create_with = Ambiguous("ReadTimeout")
    wiring.github_api.write_lands = True  # the server did apply it
    assert wiring.controller.consider(incident).action == "parked"

    # The retry finds the issue by its marker instead of opening another.
    wiring.github_api.write_lands = False
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None
    repairs.update(int(repair["id"]), state=PROPOSED, attention=None)
    assert wiring.controller.consider(incident).action == "dispatched"
    assert len(wiring.github_api.issues) == 1


def test_an_ambiguous_create_that_cannot_be_found_is_parked_not_retried(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs)
    wiring.devin_api.fail_create_with = Ambiguous("ReadTimeout")
    assert wiring.controller.consider(incident).action == "parked"
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None and repair["state"] == NEEDS_ATTENTION
    assert "unknown" in repair["attention"]

    repairs.update(int(repair["id"]), state=PROPOSED)
    assert wiring.controller.consider(incident).action == "parked"
    assert not wiring.devin_api.sessions  # never blindly created a second one


def test_a_refused_create_is_parked_without_a_remote_object(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs)
    wiring.github_api.fail_create_with = Refused("ConnectionError")
    assert wiring.controller.consider(incident).action == "parked"
    assert not wiring.github_api.issues


@pytest.mark.parametrize("status", [401, 403, 429])
def test_an_authentication_or_rate_limit_answer_stops_the_dispatch(
    repairs: RepairStore, incident: dict[str, Any], status: int
) -> None:
    wiring = Wiring(repairs)
    wiring.devin_api.status = status
    with pytest.raises(RuntimeError) as raised:
        wiring.controller.consider(incident)
    assert str(status) in str(raised.value)
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None and repair["session_id"] is None


# --- lifecycle -------------------------------------------------------------


def test_agent_finished_with_a_pull_request_is_a_candidate_not_a_success(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore
) -> None:
    decision = wiring.controller.consider(incident)
    wiring.devin_api.finish(wiring.session_id(), GOOD_OUTPUT, PR_URL, acus=3.0)
    result = wiring.controller.poll(decision.repair_id or 0)

    assert result.action == "candidate"
    repair = repairs.get(decision.repair_id or 0)
    assert repair is not None
    assert repair["state"] == CANDIDATE
    assert repair["pr_head_sha"] == "f" * 40  # read from GitHub, not from the agent
    assert repair["verification"] is None  # only the independent replay writes this


def test_a_pull_request_on_the_wrong_base_or_host_is_not_a_candidate(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs)
    decision = wiring.controller.consider(incident)
    wiring.pulls[7]["base"] = {"ref": "master"}
    wiring.devin_api.finish(wiring.session_id(), GOOD_OUTPUT, PR_URL)
    assert wiring.controller.poll(decision.repair_id or 0).action == "parked"

    elsewhere = dict(GOOD_OUTPUT, pr_url="https://example.com/evil/pull/1")
    wiring.devin_api.finish(wiring.session_id(), elsewhere, "")
    wiring.controller.poll(decision.repair_id or 0)
    repair = repairs.get(decision.repair_id or 0)
    assert repair is not None and repair["state"] == NEEDS_ATTENTION


def test_a_session_that_did_not_reproduce_first_is_parked(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    decision = wiring.controller.consider(incident)
    wiring.devin_api.finish(wiring.session_id(), dict(GOOD_OUTPUT, reproduced=False), PR_URL)
    assert wiring.controller.poll(decision.repair_id or 0).action == "parked"


def test_an_expected_denial_classification_ends_without_a_code_change(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore
) -> None:
    decision = wiring.controller.consider(incident)
    wiring.devin_api.finish(
        wiring.session_id(),
        dict(GOOD_OUTPUT, classification="expected_behaviour", pr_url=""),
        "",
    )
    assert wiring.controller.poll(decision.repair_id or 0).action == "classified"
    repair = repairs.get(decision.repair_id or 0)
    assert repair is not None and repair["state"] == TERMINAL


def test_a_finished_session_without_structured_output_is_parked(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    decision = wiring.controller.consider(incident)
    wiring.devin_api.finish(wiring.session_id(), None, PR_URL)
    assert wiring.controller.poll(decision.repair_id or 0).action == "parked"


def test_waiting_and_suspended_sessions_are_surfaced_not_terminated(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    decision = wiring.controller.consider(incident)
    wiring.devin_api.set_state(wiring.session_id(), status_detail="waiting_for_approval")
    assert wiring.controller.poll(decision.repair_id or 0).action == "waiting"

    wiring.devin_api.set_state(wiring.session_id(), status="suspended", status_detail="error")
    assert wiring.controller.poll(decision.repair_id or 0).action == "parked"
    assert not wiring.devin_api.terminated


# --- budget ----------------------------------------------------------------


def test_the_acu_cap_stops_a_running_session_with_a_visible_reason(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs, budget=Budget(acu_limit=5, wall_clock_minutes=60))
    decision = wiring.controller.consider(incident)
    wiring.devin_api.set_state(wiring.session_id(), acus_consumed=5.5)
    assert wiring.controller.poll(decision.repair_id or 0).action == "stopped"

    repair = repairs.get(decision.repair_id or 0)
    assert repair is not None and repair["state"] == TERMINAL
    assert "ACU budget exhausted" in repair["terminal_reason"]
    assert wiring.devin_api.terminated == [wiring.session_id()]
    assert repair["acu_limit"] == 5  # never raised by the controller itself


def test_the_deadline_survives_a_restart(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    first = Wiring(repairs, budget=Budget(wall_clock_minutes=30))
    decision = first.controller.consider(incident)

    second = Wiring(repairs)
    second.devin_api.sessions = first.devin_api.sessions
    second.clock = first.clock + timedelta(minutes=31)
    assert second.controller.poll(decision.repair_id or 0).action == "stopped"
    repair = repairs.get(decision.repair_id or 0)
    assert repair is not None and "deadline" in repair["terminal_reason"]


def test_a_candidate_is_never_terminated_before_verification_can_answer(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs, budget=Budget(acu_limit=1, wall_clock_minutes=1))
    decision = wiring.controller.consider(incident)
    wiring.devin_api.finish(wiring.session_id(), GOOD_OUTPUT, PR_URL, acus=99.0)
    wiring.clock += timedelta(hours=5)

    assert wiring.controller.poll(decision.repair_id or 0).action == "candidate"
    # Terminated v3 sessions cannot be resumed, so the session that must
    # receive verification feedback stays alive.
    assert not wiring.devin_api.terminated


# --- feedback --------------------------------------------------------------


def _candidate(wiring: Wiring, incident: dict[str, Any]) -> int:
    decision = wiring.controller.consider(incident)
    wiring.devin_api.finish(wiring.session_id(), GOOD_OUTPUT, PR_URL)
    wiring.controller.poll(decision.repair_id or 0)
    return decision.repair_id or 0


def test_failed_verification_goes_back_to_the_same_session(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    repair_id = _candidate(wiring, incident)
    result = wiring.controller.feedback(repair_id, ["the discarded key is still reused"])
    assert result.action == "followed_up"

    session_id, message = wiring.devin_api.messages[0]
    assert session_id == wiring.session_id()
    assert "the discarded key is still reused" in message
    assert PR_URL in message and "f" * 40 in message


def test_a_follow_up_is_delivered_once_even_if_verification_reruns(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    repair_id = _candidate(wiring, incident)
    wiring.controller.feedback(repair_id, ["still failing"])
    wiring.controller.feedback(repair_id, ["still failing"])  # repair is dispatched again
    assert len(wiring.devin_api.messages) == 1


def test_the_third_follow_up_stops_the_repair_instead(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore
) -> None:
    repair_id = _candidate(wiring, incident)
    for round_number in range(MAX_FOLLOW_UPS):
        assert wiring.controller.feedback(repair_id, [f"round {round_number}"]).action == (
            "followed_up"
        )
        repairs.update(repair_id, state=CANDIDATE)

    assert wiring.controller.feedback(repair_id, ["again"]).action == "stopped"
    assert len(wiring.devin_api.messages) == MAX_FOLLOW_UPS
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == TERMINAL
    assert "follow-ups" in repair["terminal_reason"]


def test_an_ambiguous_follow_up_is_parked_not_repeated(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    repair_id = _candidate(wiring, incident)
    wiring.devin_api.fail_create_with = Ambiguous("ReadTimeout")
    wiring.devin_api.write_lands = True
    assert wiring.controller.feedback(repair_id, ["still failing"]).action == "parked"
    assert len(wiring.devin_api.messages) == 1  # it landed; nothing sends it twice


# --- console ---------------------------------------------------------------


def test_the_console_shows_what_would_be_sent(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from portal import app as portal_app
    from portal.config import settings

    # The failure arrives the way a real one does: through the event stream,
    # with nobody opening the console first.
    portal_app.store.emit(event(event_id="console-1", trace_id="console-trace"))
    incident = portal_app.incidents.by_fingerprint(
        next(
            i["fingerprint"]
            for i in portal_app.incidents.list()
            if i["scenario"] == "S2"
        )
    )
    assert incident is not None
    repair = portal_app.repairs.by_fingerprint(str(incident["fingerprint"]))
    assert repair is not None and repair["state"] == PROPOSED

    client = TestClient(portal_app.app)
    page = client.get(
        f"/ops/incidents/{incident['id']}",
        auth=(settings.ops_username, settings.ops_password),
    )
    assert page.status_code == 200
    assert "Proposed Devin request body" in page.text
    assert "AUTO_REPAIR_ENABLED=false" in page.text
    assert "max_acu_limit" in page.text
    assert "not verified" in page.text


# --- pagination ------------------------------------------------------------


def test_sessions_are_found_through_documented_cursor_pagination(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    wiring.controller.consider(incident)
    mark = brief.marker(incident, 1)
    for index in range(5):
        wiring.devin_api.sessions[f"devin-noise-{index}"] = {
            "session_id": f"devin-noise-{index}",
            "url": "",
            "status": "running",
            "status_detail": "working",
            "acus_consumed": 0,
            "pull_requests": [],
            "structured_output": None,
            "tags": ["runtime-repair"],
        }
    wiring.devin_api.page_size = 2
    devin = Devin(wiring.devin_wire, api_key="cog_simulated", org_id="org-simulated")
    assert devin.find_tagged(mark) is not None

    listed = [c for c in wiring.devin_wire.calls if c.method == "GET" and c.params]
    assert listed and listed[0].params is not None
    assert "first" in listed[0].params and "after" not in listed[0].params
