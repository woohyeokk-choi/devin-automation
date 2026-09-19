"""The repair loop, driven entirely by simulated providers.

Every test here runs against `portal.simulation`: no network, no credentials,
no GitHub issue and no Devin session. What is real is the client and
controller code under test — the fakes only stand in for the wire.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from portal import brief
from portal.controller import (
    CANDIDATE,
    Decision,
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


class Wiring:
    """A controller with both providers faked, plus the fakes to drive them."""

    def __init__(self, repairs: RepairStore, **kwargs: Any) -> None:
        #: What the worker re-reads a queued proposal's incident from.
        self.incidents: dict[int, dict[str, Any]] = {}
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
            incident_of=self.incidents.get,
            **kwargs,
        )
        self.pulls[7] = {
            "head": {"sha": "f" * 40, "repo": {"full_name": REPO}},
            "base": {"ref": brief.BASE_BRANCH},
            "state": "open",
            "merged": False,
        }

    def dispatch(self, incident: dict[str, Any]) -> Decision:
        """What production does: the request queues, the worker dispatches.

        Returns the decision *for this repair*, so a proposal that had to wait
        behind the claim holder reads as `deferred` rather than as whatever
        the worker did with the repair already in flight.
        """
        self.incidents[int(incident["id"])] = incident
        decision = self.controller.consider(incident)
        if decision.action != "queued":
            return decision
        for taken in self.controller.advance():
            if taken.repair_id == decision.repair_id:
                return taken
        return Decision("deferred", "another repair holds the claim", decision.repair_id)

    def session_id(self) -> str:
        return next(iter(self.devin_api.sessions))


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
    # A handoff has to stand on its own: the bootstrap may name the session's
    # own local ports, but the evidence and the steps must be in the prompt,
    # not behind a link to this machine's console.
    prompt = json.loads(request)["prompt"]
    assert "/ops/incidents" not in prompt
    for carried in (BASELINE, "docker compose", "Reproduce this case", "row limit"):
        assert carried in prompt, carried
    # The deadline only starts when live work does.
    assert repair["deadline_utc"] == ""


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
    wiring.dispatch(incident)
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None and repair["simulated"] == 1
    assert SIMULATED in repair["session_url"]
    # The incident store knows nothing of it: no verified count can move.
    assert incident["state"] == "detected" and incident["verification"] is None


# --- dispatch --------------------------------------------------------------


def test_dispatch_creates_one_issue_and_one_session(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore
) -> None:
    decision = wiring.dispatch(incident)
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
    assert wiring.dispatch(blocked).action == "skipped"
    assert not wiring.github_api.issues and not wiring.devin_api.sessions


def test_only_one_repair_runs_at_a_time(
    wiring: Wiring, incident: dict[str, Any], tmp_path: Path
) -> None:
    wiring.dispatch(incident)
    other = IncidentStore(tmp_path / "s1.sqlite", target_repo=REPO)
    other.observe(
        event(event_id="x1", assertion="row_limit_survives_an_unrelated_change", trace_id="t9")
    )
    second = other.get(1)
    assert second is not None
    assert wiring.dispatch(second).action == "deferred"
    assert len(wiring.devin_api.sessions) == 1


def test_a_repeated_delivery_does_not_create_a_second_session(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    wiring.dispatch(incident)
    for _ in range(3):
        assert wiring.dispatch(incident).action == "in_flight"
    assert len(wiring.github_api.issues) == 1 and len(wiring.devin_api.sessions) == 1


def test_concurrent_delivery_creates_one_repair(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    import threading

    actions: list[str] = []
    barrier = threading.Barrier(4)

    def deliver() -> None:
        barrier.wait()
        actions.append(wiring.dispatch(incident).action)

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
    first.dispatch(incident)

    # New process, same durable store, same remote state.
    second = Wiring(repairs)
    second.github_api.issues = first.github_api.issues
    second.devin_api.sessions = first.devin_api.sessions
    assert second.dispatch(incident).action == "in_flight"
    assert len(second.github_api.issues) == 1


def test_an_ambiguous_create_that_landed_is_reconciled_by_its_marker(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs)
    wiring.github_api.fail_create_with = Ambiguous("ReadTimeout")
    wiring.github_api.write_lands = True  # the server did apply it
    assert wiring.dispatch(incident).action == "parked"

    # The retry finds the issue by its marker instead of opening another.
    wiring.github_api.write_lands = False
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None
    repairs.update(int(repair["id"]), state=PROPOSED, attention=None)
    # An operator releasing a parked repair hands it straight back to the
    # dispatcher; it already holds the claim.
    assert wiring.controller.dispatch(int(repair["id"]), incident).action == "dispatched"
    assert len(wiring.github_api.issues) == 1


def test_an_ambiguous_create_that_cannot_be_found_is_parked_not_retried(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs)
    wiring.devin_api.fail_create_with = Ambiguous("ReadTimeout")
    assert wiring.dispatch(incident).action == "parked"
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None and repair["state"] == NEEDS_ATTENTION
    assert "unknown" in repair["attention"]

    repairs.update(int(repair["id"]), state=PROPOSED)
    assert wiring.controller.dispatch(int(repair["id"]), incident).action == "parked"
    assert not wiring.devin_api.sessions  # never blindly created a second one


def test_a_refused_create_is_parked_without_a_remote_object(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs)
    wiring.github_api.fail_create_with = Refused("ConnectionError")
    assert wiring.dispatch(incident).action == "parked"
    assert not wiring.github_api.issues


@pytest.mark.parametrize("status", [401, 403, 429])
def test_an_authentication_or_rate_limit_answer_stops_the_dispatch(
    repairs: RepairStore, incident: dict[str, Any], status: int
) -> None:
    wiring = Wiring(repairs)
    wiring.devin_api.status = status
    with pytest.raises(RuntimeError) as raised:
        wiring.dispatch(incident)
    assert str(status) in str(raised.value)
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None and repair["session_id"] is None


# --- lifecycle -------------------------------------------------------------


def test_agent_finished_with_a_pull_request_is_a_candidate_not_a_success(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore
) -> None:
    decision = wiring.dispatch(incident)
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
    decision = wiring.dispatch(incident)
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
    decision = wiring.dispatch(incident)
    wiring.devin_api.finish(wiring.session_id(), dict(GOOD_OUTPUT, reproduced=False), PR_URL)
    assert wiring.controller.poll(decision.repair_id or 0).action == "parked"


def test_an_expected_denial_classification_ends_without_a_code_change(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore
) -> None:
    decision = wiring.dispatch(incident)
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
    decision = wiring.dispatch(incident)
    wiring.devin_api.finish(wiring.session_id(), None, PR_URL)
    assert wiring.controller.poll(decision.repair_id or 0).action == "parked"


def test_waiting_and_suspended_sessions_are_surfaced_not_terminated(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    decision = wiring.dispatch(incident)
    wiring.devin_api.set_state(wiring.session_id(), status_detail="waiting_for_approval")
    assert wiring.controller.poll(decision.repair_id or 0).action == "waiting"

    wiring.devin_api.set_state(wiring.session_id(), status="suspended", status_detail="error")
    assert wiring.controller.poll(decision.repair_id or 0).action == "parked"
    assert not wiring.devin_api.terminated


def test_a_question_after_the_result_still_yields_a_candidate(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    decision = wiring.dispatch(incident)
    wiring.devin_api.finish(wiring.session_id(), GOOD_OUTPUT, PR_URL)
    wiring.devin_api.set_state(wiring.session_id(), status_detail="waiting_for_user")

    assert wiring.controller.poll(decision.repair_id or 0).action == "candidate"


# --- budget ----------------------------------------------------------------


def test_the_acu_cap_stops_a_running_session_with_a_visible_reason(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs, budget=Budget(acu_limit=5, wall_clock_minutes=60))
    decision = wiring.dispatch(incident)
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
    decision = first.dispatch(incident)

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
    decision = wiring.dispatch(incident)
    wiring.devin_api.finish(wiring.session_id(), GOOD_OUTPUT, PR_URL, acus=99.0)
    wiring.clock += timedelta(hours=5)

    assert wiring.controller.poll(decision.repair_id or 0).action == "candidate"
    # Terminated v3 sessions cannot be resumed, so the session that must
    # receive verification feedback stays alive.
    assert not wiring.devin_api.terminated


# --- feedback --------------------------------------------------------------


def _candidate(wiring: Wiring, incident: dict[str, Any]) -> int:
    decision = wiring.dispatch(incident)
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
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore
) -> None:
    """Polling the same candidate commit twice is one message, not two."""
    repair_id = _candidate(wiring, incident)
    wiring.controller.feedback(repair_id, ["still failing"])
    repairs.update(repair_id, state=CANDIDATE)
    wiring.controller.feedback(repair_id, ["still failing"])
    assert len(wiring.devin_api.messages) == 1


def test_the_third_follow_up_stops_the_repair_instead(
    wiring: Wiring, incident: dict[str, Any], repairs: RepairStore
) -> None:
    repair_id = _candidate(wiring, incident)
    # Each round is a new commit on the same pull request: the agent pushed a
    # fix, verification ran again on that commit, and it still fails.
    for round_number in range(MAX_FOLLOW_UPS):
        sha = str(round_number) * 40
        assert wiring.controller.feedback(
            repair_id, [f"round {round_number}"], sha
        ).action == "followed_up"
        repairs.update(repair_id, state=CANDIDATE)

    assert wiring.controller.feedback(repair_id, ["again"], "a" * 40).action == "stopped"
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
    wiring.dispatch(incident)
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


# --- single-flight ---------------------------------------------------------


def second_incident(tmp_path: Path) -> dict[str, Any]:
    """A distinct eligible incident: the S1 family, not another S2 delivery."""
    store = IncidentStore(tmp_path / "incidents-s1.sqlite", target_repo=REPO)
    store.observe(
        event(
            event_id="s1-1",
            trace_id="s1-trace",
            assertion="row_limit_survives_an_unrelated_change",
        )
    )
    found = store.get(1)
    assert found is not None
    return found


def test_two_distinct_incidents_cannot_both_dispatch_through_two_connections(
    tmp_path: Path, incident: dict[str, Any]
) -> None:
    """The claim is the database's, not one process's read of `active()`."""
    db = tmp_path / "shared-repairs.sqlite"
    first, second = RepairStore(db, simulated=True), RepairStore(db, simulated=True)
    other = second_incident(tmp_path)

    one, two = Wiring(first), Wiring(second)
    # One shared GitHub and one shared Devin behind two clients: a second
    # session appearing here is a second paid repair.
    two.devin_wire.handler = one.devin_api
    two.github_wire.handler = one.github_api
    two.devin_api, two.github_api = one.devin_api, one.github_api
    two.pulls = one.pulls
    # Both workers read incidents from the same store, as two processes of one
    # deployment do. Giving each its own view would let either worker decide
    # the other's proposal refers to a missing incident and free the slot.
    two.incidents = one.incidents
    two.controller.incident_of = one.incidents.get

    start = threading.Barrier(2)
    creating = threading.Event()
    serve = one.devin_api

    def hold(call: Any) -> Any:
        if call.method == "POST" and call.url.endswith("/sessions"):
            # Hold the winner inside create_session so the loser races
            # against a claim that is taken but not yet dispatched.
            creating.set()
            time.sleep(0.2)
        return serve(call)

    one.devin_wire.handler = hold
    two.devin_wire.handler = hold

    results: list[Any] = []

    def run(wire: Wiring, case: dict[str, Any]) -> None:
        start.wait(timeout=5)
        results.append(wire.dispatch(case))

    threads = [
        threading.Thread(target=run, args=(w, i))
        for w, i in ((one, incident), (two, other))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    # Which worker wins, and whose proposal it takes up, is a race: a worker
    # may well dispatch the repair the other thread proposed. What cannot
    # happen is two of them getting past the claim, so the invariant is
    # counted in the database and at the provider, not in who was told what.
    actions = sorted(d.action for d in results)
    assert set(actions) <= {"dispatched", "deferred", "in_flight"}, actions
    assert creating.is_set()
    assert len(one.devin_api.sessions) == 1
    assert len(one.github_api.issues) == 1
    dispatched = [r for r in first.list() if r["state"] == DISPATCHED]
    assert len(dispatched) == 1, [r["state"] for r in first.list()]
    assert first.slot_holder() == int(dispatched[0]["id"])
    second.close()
    first.close()


def test_the_claim_survives_a_restart(tmp_path: Path, incident: dict[str, Any]) -> None:
    db = tmp_path / "restart-repairs.sqlite"
    store = RepairStore(db, simulated=True)
    Wiring(store).dispatch(incident)
    holder = store.slot_holder()
    store.close()

    reopened = RepairStore(db, simulated=True)
    assert reopened.slot_holder() == holder
    # A different incident arriving after the restart still waits.
    fresh = Wiring(reopened)
    decision = fresh.dispatch(second_incident(tmp_path))
    assert decision.action == "deferred"
    assert not fresh.devin_api.sessions
    reopened.close()


def test_an_unknown_termination_outcome_keeps_the_claim(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    wiring.dispatch(incident)
    repair_id = wiring.controller.store.active()["id"]
    # The terminate call answers with a server error: whether the session was
    # stopped is unknown.
    wiring.devin_api.status = 500
    wiring.controller._stop(int(repair_id), "budget spent")

    repair = wiring.controller.store.get(int(repair_id))
    assert repair is not None and repair["state"] == TERMINAL
    assert "may still be running" in (repair["attention"] or "")
    assert wiring.controller.store.slot_holder() == int(repair_id)


def test_a_finished_classification_releases_the_claim(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    wiring.dispatch(incident)
    repair_id = int(wiring.controller.store.active()["id"])
    wiring.devin_api.finish(
        wiring.session_id(),
        {**GOOD_OUTPUT, "classification": "configuration", "pr_url": ""},
    )
    assert wiring.controller.poll(repair_id).action == "classified"
    assert wiring.controller.store.slot_holder() is None


# --- budget ----------------------------------------------------------------


def test_a_follow_up_is_refused_once_the_deadline_has_passed(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs, budget=Budget(acu_limit=5, wall_clock_minutes=1))
    wiring.dispatch(incident)
    repair_id = int(repairs.active()["id"])
    wiring.devin_api.finish(wiring.session_id(), GOOD_OUTPUT)
    assert wiring.controller.poll(repair_id).action == "candidate"

    wiring.clock += timedelta(minutes=2)
    decision = wiring.controller.feedback(repair_id, ["still reuses the key"])

    assert decision.action == "stopped"
    assert not wiring.devin_api.messages
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == TERMINAL
    assert "deadline" in (repair["terminal_reason"] or "")


def test_a_follow_up_is_refused_once_the_acu_limit_is_spent(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    wiring = Wiring(repairs, budget=Budget(acu_limit=2, wall_clock_minutes=600))
    wiring.dispatch(incident)
    repair_id = int(repairs.active()["id"])
    wiring.devin_api.finish(wiring.session_id(), GOOD_OUTPUT, acus=2.0)
    assert wiring.controller.poll(repair_id).action == "candidate"

    decision = wiring.controller.feedback(repair_id, ["still reuses the key"])

    assert decision.action == "stopped"
    assert not wiring.devin_api.messages
    assert "ACU" in decision.detail


def test_the_deadline_starts_when_the_session_does_not_when_it_is_proposed(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    disabled = Controller(
        repairs, target_repo=REPO, versions=VERSIONS, dispatch_enabled=False
    )
    disabled.consider(incident)
    assert repairs.by_fingerprint(incident["fingerprint"])["deadline_utc"] == ""

    wiring = Wiring(repairs, budget=Budget(acu_limit=5, wall_clock_minutes=30))
    wiring.clock += timedelta(hours=6)
    wiring.dispatch(incident)
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None
    started = datetime.fromisoformat(str(repair["deadline_utc"]))
    assert started == wiring.clock + timedelta(minutes=30)


# --- pull request validation -----------------------------------------------


@pytest.mark.parametrize(
    "head, reason",
    [
        ({"repo": {"full_name": "untrusted-owner/untrusted-fork"}}, "untrusted-fork"),
        ({"sha": "f" * 7}, "full commit id"),
        ({"sha": ""}, "full commit id"),
    ],
)
def test_an_unusable_pull_request_never_becomes_a_candidate(
    wiring: Wiring, incident: dict[str, Any], head: dict[str, Any], reason: str
) -> None:
    wiring.dispatch(incident)
    wiring.pulls[7]["head"].update(head)
    wiring.devin_api.finish(wiring.session_id(), GOOD_OUTPUT)

    decision = wiring.controller.poll(int(wiring.controller.store.active()["id"]))

    assert decision.action == "parked"
    assert reason in decision.detail
    repair = wiring.controller.store.get(decision.repair_id or 0)
    assert repair is not None and repair["state"] == NEEDS_ATTENTION
    assert repair["pr_head_sha"] is None


@pytest.mark.parametrize(
    "pull, reason",
    [({"state": "closed"}, "closed"), ({"merged": True}, "already merged")],
)
def test_a_closed_or_merged_pull_request_is_not_verifiable(
    wiring: Wiring, incident: dict[str, Any], pull: dict[str, Any], reason: str
) -> None:
    wiring.dispatch(incident)
    wiring.pulls[7].update(pull)
    wiring.devin_api.finish(wiring.session_id(), GOOD_OUTPUT)

    decision = wiring.controller.poll(int(wiring.controller.store.active()["id"]))

    assert decision.action == "parked" and reason in decision.detail


# --- reconciliation --------------------------------------------------------


def test_a_session_without_this_attempts_tag_is_never_adopted(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    mark = brief.marker(incident, 1)
    wiring.devin_api.sessions["devin-unrelated"] = {
        "session_id": "devin-unrelated",
        "url": "",
        "status": "running",
        "status_detail": "working",
        "acus_consumed": 0,
        "pull_requests": [],
        "structured_output": None,
        "tags": ["runtime-repair"],
    }
    devin = Devin(wiring.devin_wire, api_key="cog_simulated", org_id="org-simulated")
    assert devin.find_tagged(mark) is None


def test_a_live_shaped_session_id_is_adopted_after_an_ambiguous_create(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    """The v3 API answers with a bare identifier, not a `devin-` prefixed one.

    Reconciliation that only recognised the prefixed spelling would miss the
    session it had just created and open a second paid one.
    """
    mark = brief.marker(incident, 1)
    bare = "fe54a690c0f146a6ad19f87e5b9a6d7e"
    wiring.devin_api.sessions[bare] = {
        "session_id": bare,
        "url": f"https://app.devin.ai/sessions/{bare}",
        "status": "running",
        "status_detail": "working",
        "acus_consumed": 0,
        "pull_requests": [],
        "structured_output": None,
        "tags": ["runtime-repair", mark],
    }
    devin = Devin(wiring.devin_wire, api_key="cog_simulated", org_id="org-simulated")

    found = devin.find_tagged(mark)

    assert found is not None and found.session_id == bare


def test_a_pull_request_is_not_reused_as_the_tracking_issue(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    mark = brief.marker(incident, 1)
    wiring.github_api.issues.append(
        {
            "number": 41,
            "html_url": f"https://github.com/{REPO}/pull/41",
            "state": "open",
            "body": f"<!-- {mark} -->",
            "pull_request": {"url": "..."},
        }
    )
    github = GitHub(wiring.github_wire, token="simulated-token", repo=REPO)
    assert github.find_issue(mark, brief.LABELS) is None


# --- a stalled creation claim ----------------------------------------------


def test_a_creation_claim_whose_worker_died_becomes_visible_work(
    wiring: Wiring, incident: dict[str, Any]
) -> None:
    repair = wiring.controller._propose(incident)
    wiring.controller.store.intend(int(repair["id"]), "issue", brief.marker(incident, 1))

    # Intents are stamped with the real clock, so age this one against it.
    wiring.clock = datetime.now(timezone.utc) + timedelta(hours=1)
    decisions = wiring.controller.recover()

    assert [d.action for d in decisions] == ["parked"]
    stalled = wiring.controller.store.get(int(repair["id"]))
    assert stalled is not None and stalled["state"] == NEEDS_ATTENTION
    assert "never confirmed" in (stalled["attention"] or "")
