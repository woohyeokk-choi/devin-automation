"""Independent verification, with simulated candidates and no live stack.

Everything here is labelled simulated: the pull requests are in-memory, the
"candidate stack" is a fake runner, and the replay report is a fixture. What
is real is the policy under test — change scope, provenance, evidence, the
verdict-to-action mapping and the bounded feedback loop.

No real pull request URL, no real commit and no real verified count is
produced by any test in this file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from portal.controller import (
    CANDIDATE,
    NEEDS_ATTENTION,
    RepairStore,
    VERIFIED,
)
from portal.isolation import SECRET_NAMES, safe_environment
from portal.providers import GitHub
from portal.validator import BLOCKED, FAILED, PASSED
from portal.verification import (
    Environment,
    RunnerError,
    VerificationStore,
    Verifier,
    check_scope,
    provenance_problem,
)

from test_controller import Wiring, _candidate
from test_incidents import REPO

CANDIDATE_SHA = "f" * 40
BASE_BRANCH = "runtime-repair/baseline"


# --- change scope ----------------------------------------------------------


def test_a_product_fix_with_its_test_is_in_scope() -> None:
    verdict = check_scope(
        [
            "superset/explore/form_data/commands/delete.py",
            "tests/unit_tests/explore/test_form_data.py",
        ]
    )
    assert verdict.allowed and not verdict.reasons


@pytest.mark.parametrize(
    "path",
    [
        "portal/validator.py",          # the thing that grades it
        "scripts/seed_synthetic.py",    # the fixtures
        "superset/security/manager.py",  # authentication and authorisation
        ".github/workflows/ci.yml",     # what runs in CI
        "docker/docker-bootstrap.sh",   # how the stack starts
        "requirements/base.txt",        # dependencies
        "README.md",                    # not product code
    ],
)
def test_a_candidate_that_reaches_outside_product_code_is_refused(path: str) -> None:
    verdict = check_scope(["superset/explore/form_data/commands/delete.py", path])
    assert not verdict.allowed
    assert any(path in reason for reason in verdict.reasons)


def test_an_empty_or_enormous_diff_is_refused() -> None:
    assert not check_scope([]).allowed
    assert not check_scope([f"superset/file{n}.py" for n in range(200)]).allowed


# --- measured provenance ---------------------------------------------------


def environment(**overrides: Any) -> Environment:
    web = {
        "source_sha": CANDIDATE_SHA,
        "source_mount": "/work/candidatefffffffffff/superset",
        "clean": True,
    }
    provenance = {
        "measured_at": "2026-09-19T12:00:00+00:00",
        "web": dict(web),
        "mcp": dict(web),
    }
    for service, changes in overrides.items():
        provenance[service] = {**provenance[service], **changes}
    return Environment(
        project="candidatefffffffffff",
        base_url="http://127.0.0.1:8288",
        mcp_url="http://127.0.0.1:5208/mcp",
        checkout="/work/candidatefffffffffff",
        head_sha=CANDIDATE_SHA,
        provenance=provenance,
    )


def test_a_stack_running_the_candidate_commit_has_no_provenance_problem() -> None:
    assert provenance_problem(environment(), CANDIDATE_SHA) == ""


def test_a_stack_running_another_commit_cannot_stand_for_this_one() -> None:
    problem = provenance_problem(environment(mcp={"source_sha": "a" * 40}), CANDIDATE_SHA)
    assert "mcp" in problem and "not " + CANDIDATE_SHA in problem


def test_a_stack_mounted_from_the_baseline_tree_is_refused() -> None:
    problem = provenance_problem(
        environment(web={"source_mount": "/home/ubuntu/repos/superset"}), CANDIDATE_SHA
    )
    assert "not the candidate checkout" in problem


def test_a_dirty_candidate_checkout_is_refused() -> None:
    assert "uncommitted" in provenance_problem(
        environment(web={"clean": False}), CANDIDATE_SHA
    )


def test_an_absent_measurement_is_not_provenance() -> None:
    bare = Environment(
        project="p", base_url="", mcp_url="", checkout="/c", head_sha=CANDIDATE_SHA
    )
    assert "no provenance" in provenance_problem(bare, CANDIDATE_SHA)


# --- credential isolation --------------------------------------------------


def test_no_controller_credential_can_travel_into_a_candidate_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dummy canaries, never a real credential.
    for name in SECRET_NAMES:
        monkeypatch.setenv(name, f"canary-{name.lower()}")
    env = safe_environment({"SUPERSET_PORT_HOST": "8288"}, {"SUPERSET_DIR": "/work/c"})
    assert not [name for name in SECRET_NAMES if name in env]
    assert "canary" not in json.dumps(env)
    assert env["SUPERSET_PORT_HOST"] == "8288"


def test_a_secret_passed_in_explicitly_is_still_refused() -> None:
    with pytest.raises(RunnerError):
        safe_environment({}, {"GITHUB_TOKEN": "canary-token"})


# --- the verifier ----------------------------------------------------------


class FakeRunner:
    """Stands in for the isolated Compose stack. Never starts anything."""

    def __init__(self, build: Any = None, error: str = "") -> None:
        self.build = build or environment
        self.error = error
        self.prepared: list[str] = []
        self.torn_down: list[str] = []

    def prepare(self, head_sha: str) -> Environment:
        self.prepared.append(head_sha)
        if self.error:
            raise RunnerError(self.error)
        return self.build()

    def teardown(self, env: Environment) -> None:
        self.torn_down.append(env.project)


def report(verdict: str, failures: tuple[str, ...] = (), cases: int = 2) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "failures": list(failures),
        "cases": [
            {"case": "S2", "verdict": verdict, "checks": [{"kind": "target", "holds": True}]},
            {"case": "N1", "verdict": PASSED, "checks": [{"kind": "target", "holds": True}]},
        ][:cases],
    }


@pytest.fixture()
def store(tmp_path: Path) -> VerificationStore:
    return VerificationStore(tmp_path / "simulated-verifications.sqlite")


def verifier_for(
    wiring: Wiring,
    store: VerificationStore,
    *,
    runner: FakeRunner | None = None,
    replay: Any = None,
    artifacts: Path | None = None,
) -> Verifier:
    return Verifier(
        github=GitHub(wiring.github_wire, token="simulated-token", repo=REPO),
        runner=runner or FakeRunner(),
        store=store,
        target_repo=REPO,
        base_branch=BASE_BRANCH,
        validator_ref="automation@abc123",
        replay=replay or (lambda env, cases: report(PASSED)),
        artifacts=artifacts,
        simulated=True,
    )


@pytest.fixture()
def candidate(wiring: Wiring, incident: dict[str, Any], repairs: RepairStore) -> dict[str, Any]:
    wiring.github_api.pull_files[7] = ["superset/explore/form_data/commands/delete.py"]
    repair_id = _candidate(wiring, incident)
    found = repairs.get(repair_id)
    assert found is not None
    return found


def test_a_clean_replay_of_the_candidate_commit_passes(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    runner = FakeRunner()
    outcome = verifier_for(wiring, store, runner=runner).verify(candidate, incident)

    assert outcome.verdict == PASSED and outcome.candidate_sha == CANDIDATE_SHA
    assert runner.prepared == [CANDIDATE_SHA] and runner.torn_down
    attempt = store.for_repair(int(candidate["id"]))[0]
    assert attempt["validator_ref"] == "automation@abc123"
    assert json.loads(attempt["cases"]) == ["S2", "N1"]
    assert attempt["simulated"] == 1


def test_a_simulated_pass_is_never_counted_as_a_verified_repair(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    verifier_for(wiring, store).verify(candidate, incident)
    assert store.verified_count() == 0


def test_a_product_contract_failure_is_reported_with_its_lines(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    lines = ("S2.new_exploration_does_not_reuse_a_discarded_key: expected False, observed True",)
    outcome = verifier_for(
        wiring, store, replay=lambda env, cases: report(FAILED, lines)
    ).verify(candidate, incident)

    assert outcome.verdict == FAILED
    assert outcome.failures == lines


def test_a_pass_without_an_executed_assertion_is_blocked(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    empty = {"verdict": PASSED, "cases": [], "failures": []}
    outcome = verifier_for(wiring, store, replay=lambda env, cases: empty).verify(
        candidate, incident
    )
    assert outcome.verdict == BLOCKED
    assert "no executed assertion" in " ".join(outcome.failures)


def test_a_candidate_from_a_foreign_repository_is_never_executed(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring.pulls[7]["head"]["repo"]["full_name"] = "untrusted-owner/untrusted-fork"
    runner = FakeRunner()
    outcome = verifier_for(wiring, store, runner=runner).verify(candidate, incident)

    assert outcome.verdict == BLOCKED and "untrusted-fork" in outcome.reason
    assert not runner.prepared


def test_a_candidate_that_edits_the_validator_is_blocked_before_it_runs(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    wiring.github_api.pull_files[7] = ["superset/explore/api.py", "portal/validator.py"]
    runner = FakeRunner()
    outcome = verifier_for(wiring, store, runner=runner).verify(candidate, incident)

    # Blocked, not failed: nothing ran, so this says nothing about the product.
    assert outcome.verdict == BLOCKED
    assert "may not change" in outcome.reason
    assert not runner.prepared


def test_a_stack_that_is_not_the_candidate_commit_blocks_the_verdict(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    runner = FakeRunner(build=lambda: environment(web={"source_sha": "b" * 40}))
    outcome = verifier_for(wiring, store, runner=runner).verify(candidate, incident)
    assert outcome.verdict == BLOCKED and "web service is running" in outcome.reason


def test_a_stack_mounted_from_the_wrong_tree_blocks_the_verdict(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    runner = FakeRunner(
        build=lambda: environment(mcp={"source_mount": "/home/ubuntu/repos/superset"})
    )
    outcome = verifier_for(wiring, store, runner=runner).verify(candidate, incident)
    assert outcome.verdict == BLOCKED and "not the candidate checkout" in outcome.reason


def test_a_setup_failure_is_blocked_and_never_a_product_failure(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    runner = FakeRunner(error="superset-light never became healthy")
    outcome = verifier_for(wiring, store, runner=runner).verify(candidate, incident)
    assert outcome.verdict == BLOCKED and "never became healthy" in outcome.reason


def test_a_newer_commit_cannot_inherit_an_older_pass(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    def replay(env: Environment, cases: tuple[str, ...]) -> dict[str, Any]:
        # The agent pushes again while the verification is running.
        wiring.pulls[7]["head"]["sha"] = "c" * 40
        return report(PASSED)

    outcome = verifier_for(wiring, store, replay=replay).verify(candidate, incident)
    assert outcome.verdict == BLOCKED and "moved to" in outcome.reason


def test_an_unreadable_verdict_is_blocked(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    outcome = verifier_for(
        wiring, store, replay=lambda env, cases: {"verdict": "green"}
    ).verify(candidate, incident)
    assert outcome.verdict == BLOCKED and "unreadable verdict" in " ".join(outcome.failures)


def test_one_repair_answers_for_one_defect(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    """An S2 candidate is not asked to fix the unrelated S1 defect."""
    seen: list[tuple[str, ...]] = []

    def replay(env: Environment, cases: tuple[str, ...]) -> dict[str, Any]:
        seen.append(cases)
        return report(PASSED)

    verifier_for(wiring, store, replay=replay).verify(candidate, incident)
    assert seen == [("S2", "N1")] and "S1" not in seen[0]


def test_every_attempt_is_kept_with_its_evidence(
    wiring: Wiring,
    candidate: dict[str, Any],
    incident: dict[str, Any],
    store: VerificationStore,
    tmp_path: Path,
) -> None:
    verifier = verifier_for(wiring, store, artifacts=tmp_path / "artifacts")
    verifier.verify(candidate, incident)
    verifier.replay = lambda env, cases: report(FAILED, ("S2.something: expected 1, observed 2",))
    verifier.verify(candidate, incident)

    attempts = store.for_repair(int(candidate["id"]))
    assert [a["verdict"] for a in attempts] == [PASSED, FAILED]
    for attempt in attempts:
        assert attempt["candidate_sha"] == CANDIDATE_SHA
        assert attempt["started_at"] and attempt["finished_at"]
        assert json.loads(attempt["provenance"])["measured_at"]
        assert Path(attempt["artifact_path"]).exists()


def test_an_attempt_cannot_write_an_unknown_column(store: VerificationStore) -> None:
    with pytest.raises(ValueError):
        store.record({"repair_id": 1, "dropped_table": "x"})


# --- verdict to action -----------------------------------------------------


def verifying(wiring: Wiring, store: VerificationStore, **kwargs: Any) -> Wiring:
    wiring.controller.verifier = verifier_for(wiring, store, **kwargs)
    return wiring


def test_a_real_pass_becomes_verified_in_preview_and_frees_the_slot(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any],
    store: VerificationStore, repairs: RepairStore,
) -> None:
    verifying(wiring, store)
    repair_id = int(candidate["id"])
    decision = wiring.controller.verify(repair_id)

    assert decision.action == "verified"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == VERIFIED
    assert "verified" in repair["terminal_reason"]
    assert json.loads(repair["verification"])["verdict"] == PASSED
    # The next queued repair can start.
    assert repairs.slot_holder() is None
    assert wiring.devin_api.terminated == [wiring.session_id()]


def test_a_session_that_cannot_be_confirmed_stopped_keeps_the_slot(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any],
    store: VerificationStore, repairs: RepairStore,
) -> None:
    verifying(wiring, store)
    wiring.devin_api.status = 500
    repair_id = int(candidate["id"])
    assert wiring.controller.verify(repair_id).action == "verified"

    repair = repairs.get(repair_id)
    assert repair is not None and "could not be confirmed stopped" in repair["attention"]
    assert repairs.slot_holder() == repair_id


def test_a_failure_goes_back_to_the_same_session_once(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any],
    store: VerificationStore, repairs: RepairStore,
) -> None:
    lines = ("S2.discarded_link_stays_dead_after_a_new_exploration: expected 404, observed 200",)
    verifying(wiring, store, replay=lambda env, cases: report(FAILED, lines))
    repair_id = int(candidate["id"])

    assert wiring.controller.verify(repair_id).action == "followed_up"
    repairs.update(repair_id, state=CANDIDATE)
    # Polling the same commit again must not send the same message twice.
    assert wiring.controller.verify(repair_id).action == "skipped"

    assert len(wiring.devin_api.messages) == 1
    session_id, message = wiring.devin_api.messages[0]
    assert session_id == wiring.session_id()
    assert "expected 404, observed 200" in message


def test_a_blocked_verification_asks_for_attention_instead_of_a_product_change(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any],
    store: VerificationStore, repairs: RepairStore,
) -> None:
    verifying(wiring, store, runner=FakeRunner(error="the candidate stack never started"))
    repair_id = int(candidate["id"])

    assert wiring.controller.verify(repair_id).action == "blocked"
    repair = repairs.get(repair_id)
    assert repair is not None and repair["state"] == NEEDS_ATTENTION
    assert not wiring.devin_api.messages  # nothing was asked of the session
    assert repairs.slot_holder() == repair_id  # the session is still out there


def test_a_candidate_with_no_verifier_configured_is_parked_not_passed(
    wiring: Wiring, candidate: dict[str, Any], repairs: RepairStore
) -> None:
    wiring.controller.verifier = None
    assert wiring.controller.verify(int(candidate["id"])).action == "parked"
    repair = repairs.get(int(candidate["id"]))
    assert repair is not None and repair["state"] == NEEDS_ATTENTION
    assert "no verifier" in repair["attention"]
