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
from pathlib import Path
from typing import Any

import pytest

from portal.controller import (
    AWAITING_MERGE,
    MERGED,
    NEEDS_ATTENTION,
    RepairStore,
    VERIFIED,
)
from portal.notify import POST_MERGE_ONLY, PREVIEW_ONLY, Recording, message_for, result_message, result_problem
from portal.providers import GitHub
from portal.validator import BLOCKED, PASSED
from portal.verification import POST_MERGE, PREVIEW, Environment, VerificationStore, Verifier

from test_controller import Wiring, _candidate
from test_incidents import REPO
from test_verification import CANDIDATE_SHA, FakeRunner, environment, report

DEMO_BRANCH = "runtime-repair/fresh-demo-20260919-2310"
MERGE_SHA = "ab" * 20


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
    wiring.github_api.pull_files[7] = ["superset/explore/form_data/commands/delete.py"]
    repair_id = _candidate(wiring, incident)
    wiring.controller.verifier = verifier_for(wiring, store, **kwargs)
    wiring.controller.verify(repair_id)
    return repair_id


def merge(wiring: Wiring, **overrides: Any) -> None:
    """Report the pull request the way GitHub reports a merged one."""
    wiring.pulls[7].update(
        {
            "state": "closed",
            "merged": True,
            "merge_commit_sha": MERGE_SHA,
            **overrides,
        }
    )


# --- the historical behaviour is untouched ---------------------------------


def test_without_the_gate_a_preview_pass_still_ends_the_repair(
    wiring: Wiring, incident: dict[str, Any], store: VerificationStore,
    repairs: RepairStore,
) -> None:
    wiring.github_api.pull_files[7] = ["superset/explore/form_data/commands/delete.py"]
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
    # The session is left alive for the post-merge recording work.
    assert not wiring.devin_api.terminated
    assert repairs.slot_holder() is None


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
