"""The opt-in human merge gate, with no network and no credentials.

A merge is a person's decision, so nothing here merges anything: the fake
pull request is flipped to merged the way GitHub would report it, and what is
under test is what the controller does with that report — and what it refuses
to do with a pull request that was closed, merged elsewhere, or merged from a
commit nobody previewed.

Deployments that do not configure the gate must keep behaving exactly as the
two historical repairs did, so that is asserted here too.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from portal.controller import (
    AWAITING_MERGE,
    MAX_MEDIA_POLLS,
    MEDIA_CAPTURED,
    MEDIA_DELIVERED,
    MEDIA_FAILED,
    MEDIA_PENDING,
    MEDIA_REQUESTED,
    MERGED,
    NEEDS_ATTENTION,
    RepairStore,
    TERMINAL,
    VERIFIED,
)
from portal.simulation import Ambiguous, Refused
from portal.notify import POST_MERGE_ONLY, PREVIEW_ONLY, Recording, message_for, result_message, result_problem
from portal.providers import GitHub
from portal.validator import BLOCKED, PASSED
from portal.verification import POST_MERGE, PREVIEW, Environment, VerificationStore, Verifier

from test_controller import Wiring, _candidate
from test_incidents import REPO
from test_verification import CANDIDATE_SHA, FakeRunner, environment, report

DEMO_BRANCH = "runtime-repair/fresh-demo-20260919-2310"
MERGE_SHA = "ab" * 20
BASE_TIP = "ba" * 20
REVIEWED_TREE = "7e" * 20
IN_SCOPE = ["superset/explore/form_data/commands/delete.py"]


def env_at(sha: str) -> Environment:
    """The isolated stack as it looks when rebuilt from `sha`."""
    built = environment(
        checkout={"source_sha": sha}, web={"source_sha": sha}, mcp={"source_sha": sha}
    )
    return replace(built, head_sha=sha)


@pytest.fixture()
def store(tmp_path: Path) -> VerificationStore:
    return VerificationStore(tmp_path / "simulated-verifications.sqlite")


def gated(repairs: RepairStore) -> Wiring:
    wiring = Wiring(repairs, base_branch=DEMO_BRANCH, merge_gate=True)
    wiring.pulls[7]["base"]["ref"] = DEMO_BRANCH
    return wiring


def verifier_for(
    wiring: Wiring,
    store: VerificationStore,
    *,
    runner: FakeRunner | None = None,
    replay: Any = None,
) -> Verifier:
    return Verifier(
        github=GitHub(wiring.github_wire, token="simulated-token", repo=REPO),
        runner=runner or FakeRunner(build=lambda: env_at(CANDIDATE_SHA)),
        store=store,
        target_repo=REPO,
        base_branch=DEMO_BRANCH,
        validator_ref="automation@abc123",
        replay=replay or (lambda env, cases: report(PASSED)),
        simulated=True,
    )


def previewed(
    wiring: Wiring, incident: dict[str, Any], store: VerificationStore, **kwargs: Any
) -> int:
    """A gated repair that has passed its preview and is waiting for a human."""
    wiring.github_api.pull_files[7] = list(IN_SCOPE)
    repair_id = _candidate(wiring, incident)
    wiring.controller.verifier = verifier_for(wiring, store, **kwargs)
    wiring.controller.verify(repair_id)
    return repair_id


def merge(
    wiring: Wiring,
    *,
    tree: str = REVIEWED_TREE,
    landed: list[str] | None = None,
    **overrides: Any,
) -> None:
    """Report the pull request the way GitHub reports a merged one."""
    wiring.pulls[7].update(
        {
            "state": "closed",
            "merged": True,
            "merge_commit_sha": MERGE_SHA,
            **overrides,
        }
    )
    wiring.github_api.commits[CANDIDATE_SHA] = {
        "tree": REVIEWED_TREE, "parents": [BASE_TIP]
    }
    wiring.github_api.commits[MERGE_SHA] = {"tree": tree, "parents": [BASE_TIP]}
    wiring.github_api.comparisons[f"{BASE_TIP}...{MERGE_SHA}"] = (
        IN_SCOPE if landed is None else landed
    )


# --- the historical behaviour is untouched ---------------------------------


def test_without_the_gate_a_preview_pass_still_ends_the_repair(
    wiring: Wiring, incident: dict[str, Any], store: VerificationStore,
    repairs: RepairStore,
) -> None:
    wiring.github_api.pull_files[7] = list(IN_SCOPE)
    repair_id = _candidate(wiring, incident)
    wiring.controller.verifier = Verifier(
        github=GitHub(wiring.github_wire, token="simulated-token", repo=REPO),
        runner=FakeRunner(build=lambda: env_at(CANDIDATE_SHA)),
        store=store,
        target_repo=REPO,
        base_branch="runtime-repair/baseline",
        validator_ref="automation@abc123",
        replay=lambda env, cases: report(PASSED),
        simulated=True,
    )
    assert wiring.controller.verify(repair_id).action == "verified"

    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == VERIFIED
    assert repairs.slot_holder() is None
    assert wiring.devin_api.terminated == [wiring.session_id()]


# --- the gate holds the repair open ----------------------------------------


def test_a_gated_preview_pass_waits_for_a_human_and_keeps_the_session(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)

    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == AWAITING_MERGE
    assert DEMO_BRANCH in repair["attention"]
    # Nothing is claimed as accepted, the session is still reachable, and the
    # slot is held so no second repair starts behind this one.
    assert repair["terminal_reason"] in (None, "")
    assert not wiring.devin_api.terminated
    assert repairs.slot_holder() == repair_id


def test_an_open_pull_request_is_waited_on_and_nothing_is_merged(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)

    decision = wiring.controller.check_merge(repair_id)
    assert decision.action == "awaiting_merge"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == AWAITING_MERGE
    assert repair["merge_commit_sha"] in (None, "")


def test_a_pull_request_closed_without_a_merge_is_never_a_pass(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    wiring.pulls[7].update({"state": "closed", "merged": False})

    assert wiring.controller.check_merge(repair_id).action == "parked"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == NEEDS_ATTENTION
    assert "without having been merged" in repair["attention"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"base": {"ref": "master"}}, "master"),
        ({"merge_commit_sha": "abc123"}, "not a full commit id"),
        ({"head": {"sha": "c" * 40, "repo": {"full_name": REPO}}}, "verified candidate"),
        (
            {"head": {"sha": CANDIDATE_SHA, "repo": {"full_name": "someone/else"}}},
            "someone/else",
        ),
    ],
)
def test_a_merge_that_is_not_the_awaited_merge_is_refused(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    overrides: dict[str, Any],
    expected: str,
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    merge(wiring, **overrides)

    decision = wiring.controller.check_merge(repair_id)
    assert decision.action == "parked" and expected in decision.detail
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == NEEDS_ATTENTION
    assert repair["merge_verification"] in (None, "")
    assert not store.for_repair(repair_id)[1:]  # nothing was replayed


# --- the merged commit is graded on its own --------------------------------


def test_the_merge_commit_is_rebuilt_and_replayed_before_anything_is_claimed(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring = gated(repairs)
    runner = FakeRunner(build=lambda: env_at(MERGE_SHA))
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(wiring, store, runner=runner)
    merge(wiring)

    decision = wiring.controller.check_merge(repair_id)

    assert decision.action == "merge_verified" and decision.detail == MERGE_SHA
    # The deployment was rebuilt from the merge commit, not from the preview.
    assert runner.prepared == [MERGE_SHA]
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == MERGED
    assert repair["merge_commit_sha"] == MERGE_SHA
    record = json.loads(repair["merge_verification"])
    assert record["verdict"] == PASSED and record["stage"] == POST_MERGE
    # Two attempts, and the preview's is not relabelled as the merged one.
    preview, after = store.for_repair(repair_id)
    assert (preview["stage"], preview["candidate_sha"]) == (PREVIEW, CANDIDATE_SHA)
    assert (after["stage"], after["candidate_sha"]) == (POST_MERGE, MERGE_SHA)
    # The session is left alive for the post-merge recording work, and the
    # claim is held until that recording is settled.
    assert not wiring.devin_api.terminated
    assert repair["media_state"] == MEDIA_PENDING
    assert repairs.slot_holder() == repair_id


def test_a_deployment_running_another_commit_cannot_stand_for_the_merge(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    # Rebuilt, but the containers are still running the preview commit.
    wiring.controller.verifier = verifier_for(
        wiring, store, runner=FakeRunner(build=lambda: env_at(CANDIDATE_SHA))
    )
    merge(wiring)

    decision = wiring.controller.check_merge(repair_id)

    assert decision.action == "blocked"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == NEEDS_ATTENTION
    assert json.loads(repair["merge_verification"])["verdict"] == BLOCKED
    assert MERGE_SHA[:12] in repair["attention"]


def test_a_merged_commit_that_fails_the_replay_is_reported_as_a_failure(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    from portal.validator import FAILED

    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(
        wiring,
        store,
        runner=FakeRunner(build=lambda: env_at(MERGE_SHA)),
        replay=lambda env, cases: report(
            FAILED, broken={"discarded_link_stays_dead_after_a_new_exploration": 200}
        ),
    )
    merge(wiring)

    decision = wiring.controller.check_merge(repair_id)
    assert decision.action == "merge_failed"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == NEEDS_ATTENTION
    assert "did not pass the same replay" in repair["attention"]


def test_a_merge_state_that_cannot_be_read_waits_rather_than_deciding(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    wiring.pulls.pop(7)

    decision = wiring.controller.check_merge(repair_id)
    assert decision.action == "deferred"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == AWAITING_MERGE


def test_merged_content_that_is_not_the_reviewed_content_is_refused(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    """A tree nobody previewed is a different change, however it was merged."""
    wiring = gated(repairs)
    runner = FakeRunner(build=lambda: env_at(MERGE_SHA))
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(wiring, store, runner=runner)
    merge(wiring, tree="11" * 20)

    decision = wiring.controller.check_merge(repair_id)

    assert decision.action == "blocked"
    assert "is not the previewed tree" in decision.detail
    assert runner.prepared == []  # nothing was built from it
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == NEEDS_ATTENTION


def test_a_change_the_merge_itself_brought_in_is_judged_against_the_scope(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    """The merged delta is read from Git, not from the pull request's list."""
    wiring = gated(repairs)
    runner = FakeRunner(build=lambda: env_at(MERGE_SHA))
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(wiring, store, runner=runner)
    merge(wiring, landed=[*IN_SCOPE, "portal/validator.py"])

    decision = wiring.controller.check_merge(repair_id)

    assert decision.action == "blocked"
    assert runner.prepared == []
    attempt = store.for_repair(repair_id)[-1]
    assert attempt["stage"] == POST_MERGE
    assert "portal/validator.py" in attempt["failures"]


@pytest.mark.parametrize(
    "head", [{"sha": "", "repo": {"full_name": REPO}}, {"sha": "abc123", "repo": {"full_name": REPO}}]
)
def test_a_merge_without_a_full_head_commit_is_not_treated_as_a_match(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    head: dict[str, Any],
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    merge(wiring, head=head)

    decision = wiring.controller.check_merge(repair_id)
    assert decision.action == "parked" and "full commit id" in decision.detail


# --- the budget still bounds the gate --------------------------------------


def test_an_expired_gate_stops_waiting_instead_of_running_the_merged_commit(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    """A late merge does not buy work the deadline no longer covers."""
    wiring = gated(repairs)
    runner = FakeRunner(build=lambda: env_at(MERGE_SHA))
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(wiring, store, runner=runner)
    merge(wiring)
    before = repairs.get(repair_id)
    assert before is not None
    deadline = str(before["deadline_utc"])
    wiring.clock += timedelta(hours=6)

    decision = wiring.controller.check_merge(repair_id)

    assert decision.action == "stopped" and "deadline" in decision.detail
    assert runner.prepared == []
    assert not wiring.devin_api.messages
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == TERMINAL
    # A retained session does not keep working unobserved past the deadline.
    assert wiring.devin_api.terminated == [wiring.session_id()]
    assert repairs.slot_holder() is None
    assert "stopped waiting" in repair["terminal_reason"]
    # The deadline itself is left exactly where dispatch set it.
    assert str(repair["deadline_utc"]) == deadline


# --- the session is asked for the merged-code recording --------------------


def test_the_same_session_is_asked_once_to_record_the_merged_commit(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(
        wiring, store, runner=FakeRunner(build=lambda: env_at(MERGE_SHA))
    )
    merge(wiring)

    assert wiring.controller.check_merge(repair_id).action == "merge_verified"
    # The request belongs to the media step, not to the verdict.
    assert not wiring.devin_api.messages

    assert wiring.controller.check_media(repair_id).action == "awaiting_media"
    assert len(wiring.devin_api.messages) == 1
    session, text = wiring.devin_api.messages[0]
    assert session == wiring.session_id()
    assert MERGE_SHA in text and "post-merge-" in text
    assert not wiring.devin_api.terminated

    # Polling again neither re-asks nor re-runs anything.
    assert wiring.controller.check_media(repair_id).action == "awaiting_media"
    assert len(wiring.devin_api.messages) == 1


def test_a_spent_budget_does_not_buy_the_recording_request(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(
        wiring, store, runner=FakeRunner(build=lambda: env_at(MERGE_SHA))
    )
    merge(wiring)
    # Spent on the session's own account, which is what a refresh reads.
    wiring.devin_api.set_state(wiring.session_id(), acus_consumed=99.0)

    assert wiring.controller.check_merge(repair_id).action == "stopped"
    assert not wiring.devin_api.messages


def test_a_stale_budget_is_re_read_from_the_session_at_the_gate(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    """A gate can wait for hours; the numbers from before the wait are old."""
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(
        wiring, store, runner=FakeRunner(build=lambda: env_at(MERGE_SHA))
    )
    merge(wiring)
    # The store still believes almost nothing was spent.
    repairs.update(repair_id, agent_acus=1.0)
    wiring.devin_api.set_state(wiring.session_id(), acus_consumed=40.0)

    decision = wiring.controller.check_merge(repair_id)

    assert decision.action == "stopped"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["agent_acus"] == 40.0


def test_a_session_that_cannot_be_read_at_the_gate_waits(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    merge(wiring)
    wiring.devin_api.status = 500

    decision = wiring.controller.check_merge(repair_id)

    assert decision.action == "deferred"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == AWAITING_MERGE


def test_a_refused_termination_keeps_the_claim_rather_than_losing_it(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    """If the session may still be running, the slot is not handed on."""
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    merge(wiring)
    wiring.clock += timedelta(hours=6)

    def refuse(session_id: str) -> None:
        raise Refused("this session may not be terminated")

    assert wiring.controller.devin is not None
    wiring.controller.devin.terminate_session = refuse  # type: ignore[method-assign]

    assert wiring.controller.check_merge(repair_id).action == "stopped"

    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == TERMINAL
    assert repairs.slot_holder() == repair_id


# --- the merged commit's recording -----------------------------------------


#: The clock every wiring runs on, so a capture can be stamped relative to
#: the moment its recording was asked for rather than to real time.
CLOCK = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def stamp(offset: timedelta = timedelta(0)) -> str:
    return (CLOCK + offset).strftime("%Y%m%dT%H%M%SZ")


def capture_name(sha: str = MERGE_SHA, case: str = "S1", at: str = "") -> str:
    return f"post-merge-{sha}-{at or stamp()}-{case}.mp4"


def merged_and_asked(
    wiring: Wiring, incident: dict[str, Any], store: VerificationStore, tmp_path: Path
) -> int:
    """A repair past the merge whose recording has been asked for."""
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(
        wiring, store, runner=FakeRunner(build=lambda: env_at(MERGE_SHA))
    )
    wiring.controller.media_dir = tmp_path / "media"
    merge(wiring)
    assert wiring.controller.check_merge(repair_id).action == "merge_verified"
    assert wiring.controller.check_media(repair_id).action == "awaiting_media"
    return repair_id


def test_only_the_sessions_own_capture_of_the_merged_commit_counts(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
) -> None:
    wiring = gated(repairs)
    repair_id = merged_and_asked(wiring, incident, store, tmp_path)
    session = wiring.session_id()
    # Something a person uploaded, a capture of the preview, another case.
    wiring.devin_api.add_attachment(session, capture_name(), source="user")
    wiring.devin_api.add_attachment(session, capture_name(sha=CANDIDATE_SHA))
    wiring.devin_api.add_attachment(session, capture_name(case="N1"))
    wiring.devin_api.add_attachment(session, "notes.txt", content_type="text/plain")

    decision = wiring.controller.check_media(repair_id)

    assert decision.action == "awaiting_media"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["media_state"] == MEDIA_REQUESTED
    assert not repair["media_path"]


def test_a_capture_of_the_merged_commit_is_downloaded_and_held_for_slack(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = gated(repairs)
    repair_id = merged_and_asked(wiring, incident, store, tmp_path)
    wiring.devin_api.add_attachment(wiring.session_id(), capture_name())
    fetched: list[str] = []

    def fake_download(url: str, destination: Path, **kwargs: Any) -> Path:
        fetched.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"simulated capture")
        return destination

    monkeypatch.setattr("portal.media.download", fake_download)

    decision = wiring.controller.check_media(repair_id)

    assert decision.action == "media_captured"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["media_state"] == MEDIA_CAPTURED
    assert Path(str(repair["media_path"])).is_file()
    # Still held: nothing has been delivered yet.
    assert repairs.slot_holder() == repair_id
    assert not repair["media_file_id"]

    # A second pass does not fetch it again.
    assert wiring.controller.check_media(repair_id).action == "media_captured"
    assert len(fetched) == 1

    # Only Slack's own answer closes it.
    wiring.controller.media_delivered(repair_id, "F0CSIMULATED", "stored by Slack")
    settled = repairs.get(repair_id)
    assert settled is not None
    assert settled["media_state"] == MEDIA_DELIVERED
    assert settled["media_file_id"] == "F0CSIMULATED"
    assert repairs.slot_holder() is None


def test_a_recording_is_not_waited_for_forever(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
) -> None:
    """An unanswered request fails visibly instead of holding the slot."""
    wiring = gated(repairs)
    repair_id = merged_and_asked(wiring, incident, store, tmp_path)

    for _ in range(MAX_MEDIA_POLLS + 2):
        decision = wiring.controller.check_media(repair_id)
        if decision.action != "awaiting_media":
            break

    repair = repairs.get(repair_id)
    assert repair is not None and repair["media_state"] == MEDIA_FAILED
    assert repair["state"] == MERGED  # the verdict itself still stands
    assert repairs.slot_holder() is None


def test_every_ending_of_the_recording_also_ends_the_session(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
) -> None:
    """A session kept alive only to record stops when recording is over."""
    wiring = gated(repairs)
    repair_id = merged_and_asked(wiring, incident, store, tmp_path)

    for _ in range(MAX_MEDIA_POLLS + 2):
        if wiring.controller.check_media(repair_id).action != "awaiting_media":
            break

    assert wiring.devin_api.terminated == [wiring.session_id()]


def test_a_delivered_recording_also_ends_the_session(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = gated(repairs)
    repair_id = merged_and_asked(wiring, incident, store, tmp_path)
    wiring.devin_api.add_attachment(wiring.session_id(), capture_name())
    monkeypatch.setattr("portal.media.download", _write_capture)
    assert wiring.controller.check_media(repair_id).action == "media_captured"
    assert not wiring.devin_api.terminated  # still needed until Slack answers

    wiring.controller.media_delivered(repair_id, "F0CSIMULATED", "stored by Slack")

    assert wiring.devin_api.terminated == [wiring.session_id()]
    assert repairs.slot_holder() is None


def test_a_session_that_will_not_stop_keeps_the_claim_after_delivery(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = gated(repairs)
    repair_id = merged_and_asked(wiring, incident, store, tmp_path)
    wiring.devin_api.add_attachment(wiring.session_id(), capture_name())
    monkeypatch.setattr("portal.media.download", _write_capture)
    assert wiring.controller.check_media(repair_id).action == "media_captured"

    def refuse(session_id: str) -> None:
        raise Ambiguous("the terminate call was not answered")

    assert wiring.controller.devin is not None
    wiring.controller.devin.terminate_session = refuse  # type: ignore[method-assign]

    wiring.controller.media_delivered(repair_id, "F0CSIMULATED", "stored by Slack")

    repair = repairs.get(repair_id)
    assert repair is not None and repair["media_state"] == MEDIA_DELIVERED
    assert repair["media_file_id"] == "F0CSIMULATED"
    assert "may still be running" in str(repair["attention"])
    assert repairs.slot_holder() == repair_id


def test_an_unreadable_session_does_not_outlast_the_deadline(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
) -> None:
    """Usage that cannot be read is no reason to wait past the wall clock."""
    wiring = gated(repairs)
    repair_id = merged_and_asked(wiring, incident, store, tmp_path)

    def unreadable(session_id: str) -> Any:
        raise Ambiguous("the session could not be read")

    assert wiring.controller.devin is not None
    wiring.controller.devin.get_session = unreadable  # type: ignore[method-assign]

    # Inside the deadline the pass simply waits for a readable answer.
    assert wiring.controller.check_media(repair_id).action == "deferred"

    repair = repairs.get(repair_id)
    assert repair is not None
    wiring.clock = datetime.fromisoformat(str(repair["deadline_utc"])) + timedelta(
        minutes=1
    )

    decision = wiring.controller.check_media(repair_id)

    assert decision.action == "media_failed" and "deadline" in decision.detail
    settled = repairs.get(repair_id)
    assert settled is not None and settled["media_state"] == MEDIA_FAILED
    assert settled["state"] == MERGED  # the merged verdict is untouched
    assert wiring.devin_api.terminated == [wiring.session_id()]


def test_footage_older_than_the_request_or_dated_ahead_of_it_is_refused(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
) -> None:
    wiring = gated(repairs)
    repair_id = merged_and_asked(wiring, incident, store, tmp_path)
    session = wiring.session_id()
    wiring.devin_api.add_attachment(
        session, capture_name(at=stamp(-timedelta(minutes=20)))
    )
    wiring.devin_api.add_attachment(
        session, capture_name(at=stamp(timedelta(hours=3)))
    )

    decision = wiring.controller.check_media(repair_id)

    assert decision.action == "awaiting_media"
    assert "before the merged commit's recording was requested" in decision.detail
    assert "in the future" in decision.detail
    repair = repairs.get(repair_id)
    assert repair is not None and not repair["media_path"]


def test_footage_with_nothing_to_date_it_against_is_refused(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
) -> None:
    """No reference time means no capture qualifies, rather than all of them."""
    wiring = gated(repairs)
    repair_id = merged_and_asked(wiring, incident, store, tmp_path)
    repairs.update(repair_id, media_asked_at=None, merge_verification=None)
    wiring.devin_api.add_attachment(wiring.session_id(), capture_name())

    decision = wiring.controller.check_media(repair_id)

    assert decision.action == "awaiting_media"
    assert "no readable time" in decision.detail


def test_an_ambiguous_request_is_not_sent_a_second_time(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
) -> None:
    wiring = gated(repairs)
    repair_id = previewed(wiring, incident, store)
    wiring.controller.verifier = verifier_for(
        wiring, store, runner=FakeRunner(build=lambda: env_at(MERGE_SHA))
    )
    wiring.controller.media_dir = tmp_path / "media"
    merge(wiring)
    assert wiring.controller.check_merge(repair_id).action == "merge_verified"
    wiring.devin_api.fail_create_with = Ambiguous("the send may have landed")
    wiring.devin_api.write_lands = True

    assert wiring.controller.check_media(repair_id).action == "awaiting_media"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["media_state"] == MEDIA_REQUESTED

    # The next pass reconciles rather than asking again.
    assert wiring.controller.check_media(repair_id).action == "awaiting_media"
    assert len(wiring.devin_api.messages) == 1


def test_unresolved_media_keeps_a_merged_repair_being_walked(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
) -> None:
    wiring = gated(repairs)
    merged_and_asked(wiring, incident, store, tmp_path)

    actions = [d.action for d in wiring.controller.advance()]

    assert actions == ["awaiting_media"]


def test_a_passing_post_merge_stack_is_left_running_for_the_demo(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    """The merged code has to be visible on loopback, not torn down at once."""
    wiring = gated(repairs)
    runner = FakeRunner(build=lambda: env_at(MERGE_SHA))
    repair_id = previewed(wiring, incident, store)
    verifier = verifier_for(wiring, store, runner=runner)
    verifier.retain_merged = True
    wiring.controller.verifier = verifier
    merge(wiring)

    assert wiring.controller.check_merge(repair_id).action == "merge_verified"
    assert runner.torn_down == []
    retained = json.loads(store.for_repair(repair_id)[-1]["report"])
    assert retained["retained_environment"]["source_sha"] == MERGE_SHA


def test_a_failing_post_merge_stack_is_not_left_running(
    repairs: RepairStore, incident: dict[str, Any], store: VerificationStore
) -> None:
    from portal.validator import FAILED

    wiring = gated(repairs)
    runner = FakeRunner(build=lambda: env_at(MERGE_SHA))
    repair_id = previewed(wiring, incident, store)
    verifier = verifier_for(
        wiring,
        store,
        runner=runner,
        replay=lambda env, cases: report(
            FAILED, broken={"discarded_link_stays_dead_after_a_new_exploration": 200}
        ),
    )
    verifier.retain_merged = True
    wiring.controller.verifier = verifier
    merge(wiring)

    assert wiring.controller.check_merge(repair_id).action == "merge_failed"
    assert runner.torn_down


# --- what the channel is told ----------------------------------------------


def _repair(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 3,
        "incident_id": 3,
        "state": AWAITING_MERGE,
        "attempt": 1,
        "pr_head_sha": CANDIDATE_SHA,
        "merge_commit_sha": "",
        "agent_pr_url": f"https://github.com/{REPO}/pull/7",
        "issue_url": f"https://github.com/{REPO}/issues/5",
        "session_url": "https://app.devin.ai/sessions/" + "1" * 32,
        "verification": json.dumps({"attempt_id": 4, "at": "2026-09-20T00:00:00+00:00"}),
    }
    row.update(overrides)
    return row


def test_awaiting_merge_says_a_human_has_not_merged_anything_yet() -> None:
    message = message_for("awaiting_merge", _repair(), None)
    assert message is not None
    event_id, kind, text = message
    assert kind == "awaiting_merge" and event_id.endswith(CANDIDATE_SHA)
    assert PREVIEW_ONLY in text
    assert "awaiting a human merge" in text
    assert "verified after merge" not in text and MERGE_SHA not in text


def test_the_post_merge_message_names_the_merge_commit_not_the_candidate() -> None:
    message = message_for(
        "merge_verified", _repair(state=MERGED, merge_commit_sha=MERGE_SHA), None
    )
    assert message is not None
    _, kind, text = message
    assert kind == "merge_verified"
    assert MERGE_SHA in text and POST_MERGE_ONLY in text
    assert PREVIEW_ONLY not in text


def test_a_merged_repair_may_not_quote_its_preview_attempt() -> None:
    merged = _repair(state=MERGED, merge_commit_sha=MERGE_SHA)
    preview_attempt = {
        "id": 4, "verdict": PASSED, "candidate_sha": CANDIDATE_SHA, "stage": PREVIEW,
    }
    assert "not the repair's head" in result_problem(merged, preview_attempt)

    same_commit_preview = {**preview_attempt, "candidate_sha": MERGE_SHA}
    assert "not the merged commit" in result_problem(merged, same_commit_preview)

    accepted = {**same_commit_preview, "stage": POST_MERGE}
    assert result_problem(merged, accepted) == ""


def test_post_merge_footage_has_to_be_of_the_merged_commit() -> None:
    merged = _repair(state=MERGED, merge_commit_sha=MERGE_SHA)
    attempt = {"id": 4, "verdict": PASSED, "candidate_sha": MERGE_SHA, "stage": POST_MERGE}
    of_the_candidate = Recording(
        url="https://example.com/s1.mp4",
        case="S1",
        sha=CANDIDATE_SHA,
        recorded_at="2026-09-20T00:10:00+00:00",
        scope="portal UI",
    )
    _, _, stale = result_message(merged, None, attempt, recording=of_the_candidate)
    assert "recording pending" in stale

    fresh = replace(of_the_candidate, sha=MERGE_SHA)
    _, _, text = result_message(merged, None, attempt, recording=fresh)
    assert MERGE_SHA[:12] in text and "recording pending" not in text
    assert POST_MERGE_ONLY in text


# --- the capture reaches Slack, or visibly does not ------------------------


def captured(
    wiring: Wiring,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    """A merged repair whose recording is downloaded and awaiting Slack."""
    repair_id = merged_and_asked(wiring, incident, store, tmp_path)
    wiring.devin_api.add_attachment(wiring.session_id(), capture_name())
    monkeypatch.setattr("portal.media.download", _write_capture)
    assert wiring.controller.check_media(repair_id).action == "media_captured"
    # What is under test here is how an upload's outcome is read, not the
    # refusal of scripted evidence, which `test_slack_bot` covers: this
    # lifecycle is marked real so the offer is actually made.
    _mark_real(wiring, repair_id, store)
    return repair_id


def _mark_real(wiring: Wiring, repair_id: int, store: VerificationStore) -> None:
    wiring.controller.store.simulated = False
    wiring.controller.store.update(repair_id, simulated=0)
    with store._conn as db:  # noqa: SLF001 - a fixture's own database
        db.execute(
            "UPDATE verifications SET simulated = 0 WHERE repair_id = ?", (repair_id,)
        )


def _write_capture(url: str, destination: Path, **kwargs: Any) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"simulated capture")
    return destination


def worker_over(wiring: Wiring, tmp_path: Path, bot: Any) -> Any:
    from portal.notify import NotificationLog, Notifier
    from portal.worker import RepairWorker

    return RepairWorker(
        wiring.controller,
        notifier=Notifier(log=NotificationLog(tmp_path / "notifications.sqlite"), bot=bot),
    )


def test_a_captured_recording_is_offered_to_slack_and_closed_by_its_file_id(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_slack_bot import FakeBot

    wiring = gated(repairs)
    repair_id = captured(wiring, incident, store, tmp_path, monkeypatch)
    bot = FakeBot("F0CSIMULATED")
    worker = worker_over(wiring, tmp_path, bot)

    worker._publish_capture(repair_id)

    assert len(bot.uploads) == 1
    repair = repairs.get(repair_id)
    assert repair is not None
    assert repair["media_state"] == MEDIA_DELIVERED
    assert repair["media_file_id"] == "F0CSIMULATED"
    assert repairs.slot_holder() is None

    # The ledger, not the caller, is what stops a second upload.
    worker._publish_capture(repair_id)
    assert len(bot.uploads) == 1


def test_an_upload_whose_outcome_is_unknown_is_not_called_delivered(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from portal.transport import Ambiguous as SlackAmbiguous
    from test_slack_bot import FakeBot

    wiring = gated(repairs)
    repair_id = captured(wiring, incident, store, tmp_path, monkeypatch)
    worker = worker_over(
        wiring, tmp_path, FakeBot(SlackAmbiguous("the upload may have landed"))
    )

    worker._publish_capture(repair_id)

    repair = repairs.get(repair_id)
    assert repair is not None
    assert repair["media_state"] == MEDIA_CAPTURED
    assert not repair["media_file_id"]
    assert "unknown" in repair["media_detail"]


def test_a_refused_upload_fails_the_recording_visibly(
    repairs: RepairStore,
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = gated(repairs)
    repair_id = captured(wiring, incident, store, tmp_path, monkeypatch)
    # No bot at all: a webhook cannot upload a file.
    worker = worker_over(wiring, tmp_path, None)

    worker._publish_capture(repair_id)

    repair = repairs.get(repair_id)
    assert repair is not None and repair["media_state"] == MEDIA_FAILED
    assert repair["state"] == MERGED
    assert repairs.slot_holder() is None
