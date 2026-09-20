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
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from portal import coordinator
from portal.controller import (
    CANDIDATE,
    NEEDS_ATTENTION,
    RepairStore,
    VERIFIED,
)
from portal.isolation import IsolatedStack, SECRET_NAMES, free_port, safe_environment
from portal.providers import GitHub
from portal.validator import BLOCKED, FAILED, PASSED, REQUIRED_CHECKS
from portal.verification import (
    Environment,
    Portal,
    RunnerError,
    VerificationStore,
    Verifier,
    check_scope,
    grade,
    provenance_problem,
)

from test_controller import Wiring, _candidate
from test_incidents import REPO

CANDIDATE_SHA = "f" * 40
BASE_BRANCH = "runtime-repair/baseline"


# --- change scope ----------------------------------------------------------


S2_FAMILY = "discarded_form_data_key_is_reused"
S1_FAMILY = "omitted_row_limit_is_reset"


def test_a_product_fix_with_its_test_is_in_scope() -> None:
    verdict = check_scope(
        [
            "superset/explore/form_data/commands/delete.py",
            "tests/unit_tests/explore/test_form_data.py",
        ],
        S2_FAMILY,
    )
    assert verdict.allowed and not verdict.reasons


def test_the_test_tree_mirroring_the_command_package_is_in_scope() -> None:
    verdict = check_scope(
        [
            "superset/commands/explore/form_data/create.py",
            "tests/unit_tests/commands/explore/form_data/test_create.py",
        ],
        S2_FAMILY,
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
        "superset/config.py",           # global authentication and CSRF
        "superset/app.py",              # how the whole application starts
        "superset/charts/api.py",       # another defect's code, not this one
    ],
)
def test_a_candidate_that_reaches_outside_product_code_is_refused(path: str) -> None:
    verdict = check_scope(
        ["superset/explore/form_data/commands/delete.py", path], S2_FAMILY
    )
    assert not verdict.allowed
    assert any(path in reason for reason in verdict.reasons)


def test_a_family_without_a_registered_scope_goes_to_review() -> None:
    verdict = check_scope(["superset/charts/api.py"], "something_unregistered")
    assert not verdict.allowed
    assert "needs review" in verdict.reasons[0]


def test_a_diff_of_tests_alone_repairs_nothing() -> None:
    verdict = check_scope(["tests/unit_tests/explore/test_form_data.py"], S2_FAMILY)
    assert not verdict.allowed
    assert "no product code" in verdict.reasons[0]


def test_the_row_limit_family_may_change_the_chart_command() -> None:
    assert check_scope(["superset/commands/chart/update.py"], S1_FAMILY).allowed


def test_an_empty_or_enormous_diff_is_refused() -> None:
    assert not check_scope([], S2_FAMILY).allowed
    assert not check_scope(
        [f"superset/file{n}.py" for n in range(200)], S2_FAMILY
    ).allowed


# --- measured provenance ---------------------------------------------------


CODE_HASH = "9f" * 16
NOW = datetime.now(timezone.utc)
MEASURED_AT = (NOW - timedelta(minutes=5)).isoformat()


def environment(**overrides: Any) -> Environment:
    service = {
        "service": "superset-light",
        "container_id": "c0ffee",
        "image_id": "sha256:image",
        "state": "running",
        "health": "healthy",
        "source_sha": CANDIDATE_SHA,
        "source_mount": "/work/candidatefffffffffff/superset",
        "clean": True,
        "code_hash": CODE_HASH,
        "config_path": "/app/automation_stack/superset_config_mcp.py",
    }
    provenance: dict[str, Any] = {
        "measured_at": MEASURED_AT,
        "automation_ref": "a" * 40,
        "compose_project": "candidatefffffffffff",
        "config_revision": "c0ffeec0ffeec0ff",
        "checkout": {
            "path": "/work/candidatefffffffffff",
            "source_sha": CANDIDATE_SHA,
            "clean": True,
            "code_hash": CODE_HASH,
        },
        "fixture": {"kind": "content", "rows": 600, "digest": "d1" * 16},
        "web": dict(service),
        "mcp": dict(service, service="superset-mcp-light"),
    }
    for key, changes in overrides.items():
        provenance[key] = (
            {**provenance[key], **changes} if isinstance(changes, dict) else changes
        )
    return Environment(
        project="candidatefffffffffff",
        base_url="http://127.0.0.1:8288",
        mcp_url="http://127.0.0.1:5208/mcp",
        checkout="/work/candidatefffffffffff",
        head_sha=CANDIDATE_SHA,
        provenance=provenance,
    )


def problem_with(**overrides: Any) -> str:
    return provenance_problem(environment(**overrides), CANDIDATE_SHA, now=NOW)


def test_a_stack_running_the_candidate_commit_has_no_provenance_problem() -> None:
    assert problem_with() == ""


def test_a_stack_running_another_commit_cannot_stand_for_this_one() -> None:
    problem = problem_with(mcp={"source_sha": "a" * 40})
    assert "mcp" in problem and "not " + CANDIDATE_SHA in problem


def test_a_stack_mounted_from_the_baseline_tree_is_refused() -> None:
    assert "not the candidate checkout" in problem_with(
        web={"source_mount": "/home/ubuntu/repos/superset"}
    )


def test_a_mount_that_merely_looks_like_the_checkout_is_refused() -> None:
    """``/work/candidatefffffffffff-other`` is a different tree, not a child."""
    assert "not the candidate checkout" in problem_with(
        web={"source_mount": "/work/candidatefffffffffff-other/superset"}
    )


def test_a_dirty_candidate_checkout_is_refused() -> None:
    assert "not a clean tree" in problem_with(web={"clean": False})


@pytest.mark.parametrize("clean", [None, "true", 1])
def test_a_checkout_that_does_not_say_it_is_clean_is_refused(clean: Any) -> None:
    """Absent or unparseable is not the same as clean."""
    assert "not a clean tree" in problem_with(mcp={"clean": clean})


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"measured_at": "not-a-time"}, "no readable measurement time"),
        ({"measured_at": (NOW - timedelta(hours=9)).isoformat()}, "minutes ago"),
        ({"measured_at": (NOW + timedelta(hours=1)).isoformat()}, "stamped in the future"),
        ({"compose_project": "superset"}, "Compose project"),
        ({"automation_ref": ""}, "which automation revision"),
        ({"config_revision": ""}, "which configuration"),
        ({"fixture": {"error": "synthetic_orders is not present"}}, "fixture"),
        ({"checkout": {"code_hash": ""}}, "never hashed"),
        ({"checkout": {"path": "/work/candidatefffffffffff-other"}}, "is not the"),
        ({"checkout": {"source_sha": "b" * 40}}, "candidate checkout is at"),
        ({"web": {"container_id": ""}}, "no container id"),
        ({"web": {"image_id": None}}, "no image id"),
        ({"mcp": {"state": "exited"}}, "is exited"),
        ({"mcp": {"health": "unhealthy"}}, "is unhealthy"),
        ({"mcp": {"code_hash": ""}}, "hashes to nothing"),
        ({"web": {"code_hash": "ab" * 16}}, "hashes to"),
        ({"web": {"error": "no container for this compose service"}}, "nothing usable"),
    ],
)
def test_an_unmeasured_or_mismatched_stack_is_refused(
    overrides: dict[str, Any], expected: str
) -> None:
    assert expected in problem_with(**overrides)


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


def test_a_port_another_stack_already_holds_is_stepped_around() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = int(held.getsockname()[1])

        chosen = free_port(taken)

    assert chosen != taken
    assert free_port(chosen) == chosen


class RecordingStack(IsolatedStack):
    """An IsolatedStack whose subprocesses are recorded instead of run."""

    def __init__(self, tmp_path: Path, fail_at: str = "") -> None:
        super().__init__(
            target_repo="woohyeokk-choi/superset",
            automation_dir=tmp_path / "automation",
            workspace=tmp_path / "work",
            automation_ref="a" * 40,
        )
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.fail_at = fail_at

    def _run(
        self,
        argv: list[str],
        cwd: Path,
        commands: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> str:
        self.calls.append((argv, dict(env or {})))
        Path(cwd).mkdir(parents=True, exist_ok=True)
        if self.fail_at and self.fail_at in " ".join(argv):
            raise RunnerError(f"{self.fail_at} failed")
        return ""

    def _wait(self, url: str, commands: list[str]) -> None:
        return None


def test_the_seed_runs_in_the_candidate_namespace(tmp_path: Path) -> None:
    """Without the project, the seed would target the baseline stack."""
    stack = RecordingStack(tmp_path)
    stack._seed("candidatefffffffffff", "http://127.0.0.1:8288", "http://x/mcp", [])
    argv, env = stack.calls[-1]
    assert argv[-1].endswith("seed_synthetic.py")
    assert env["SUPERSET_COMPOSE_PROJECT"] == "candidatefffffffffff"
    assert env["COMPOSE_PROJECT_NAME"] == "candidatefffffffffff"
    assert env["SUPERSET_DB_CONTAINER"] == "candidatefffffffffff-db-light-1"
    assert env["SUPERSET_WEB_CONTAINER"] == "candidatefffffffffff-superset-light-1"


def test_a_failed_preparation_removes_only_its_own_stack(tmp_path: Path) -> None:
    stack = RecordingStack(tmp_path, fail_at="seed_synthetic.py")
    with pytest.raises(RunnerError):
        stack.prepare(CANDIDATE_SHA)
    downs = [argv for argv, _ in stack.calls if "down" in argv]
    assert downs, "the candidate stack was left running"
    assert all("--project-name" in argv for argv in downs)
    assert all(
        argv[argv.index("--project-name") + 1] == f"candidate{CANDIDATE_SHA[:12]}"
        for argv in downs
    )
    assert not (tmp_path / "work" / f"candidate{CANDIDATE_SHA[:12]}").exists()


def test_a_damaged_checkout_still_gets_its_containers_removed(tmp_path: Path) -> None:
    """Compose cannot read its file from a half-deleted tree; labels remain."""
    stack = RecordingStack(tmp_path, fail_at="compose")
    project = f"candidate{CANDIDATE_SHA[:12]}"
    checkout = tmp_path / "work" / project
    checkout.mkdir(parents=True)
    stack.teardown(
        Environment(
            project=project,
            base_url="http://127.0.0.1:8288",
            mcp_url="http://127.0.0.1:5208/mcp",
            checkout=str(checkout),
            head_sha=CANDIDATE_SHA,
            provenance={},
            commands=[],
        )
    )
    filters = [argv for argv, _ in stack.calls if "--filter" in argv]
    assert filters, "nothing was removed by label"
    assert all(
        f"label=com.docker.compose.project={project}" in argv for argv in filters
    )


def test_the_retained_portal_joins_the_stack_and_carries_no_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It has to reach the retained containers, and start nothing of its own."""
    stack = RecordingStack(tmp_path)
    monkeypatch.setattr(stack, "_reaches_settings", lambda url, commands: None)
    project = f"candidate{CANDIDATE_SHA[:12]}"

    portal = stack.serve_portal(
        Environment(
            project=project,
            base_url="http://127.0.0.1:8288",
            mcp_url="http://127.0.0.1:5208/mcp",
            checkout=str(tmp_path / "work" / project),
            head_sha=CANDIDATE_SHA,
        )
    )

    argv, env = stack.calls[-1]
    assert argv[argv.index("--project-name") + 1] == project
    assert argv[-2:] == ["up", "-d"]
    assert env["SUPERSET_NETWORK"] == f"{project}_default"
    assert env["SUPERSET_CONTAINER_BASE_URL"] == "http://superset-light:8088"
    assert env["PORTAL_DATA_DIR"].endswith(f"{project}-portal-state")
    assert not [name for name in SECRET_NAMES if name in env]
    assert portal.url.endswith("/settings")
    assert "127.0.0.1" in portal.url


def test_taking_the_retained_portal_down_leaves_the_stack_it_showed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--remove-orphans` in this project would delete the merged stack."""
    stack = RecordingStack(tmp_path)
    monkeypatch.setattr(
        stack,
        "_reaches_settings",
        lambda url, commands: (_ for _ in ()).throw(RunnerError("no /settings")),
    )
    project = f"candidate{CANDIDATE_SHA[:12]}"

    with pytest.raises(RunnerError):
        stack.serve_portal(
            Environment(
                project=project,
                base_url="http://127.0.0.1:8288",
                mcp_url="http://127.0.0.1:5208/mcp",
                checkout=str(tmp_path / "work" / project),
                head_sha=CANDIDATE_SHA,
            )
        )

    portal_file = "docker-compose.portal.yml"
    downs = [
        argv
        for argv, _ in stack.calls
        if "down" in argv and any(portal_file in part for part in argv)
    ]
    assert downs, "the half-started portal was left running"
    assert all("--remove-orphans" not in argv for argv in downs)


def test_a_process_without_git_or_docker_reports_that_it_cannot_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The portal container is this case: no git, no docker, no socket."""
    monkeypatch.setattr(coordinator, "_tool", lambda *argv: "")
    report = coordinator.capability_report()
    assert report["can_verify"] is False
    assert "git" in report["missing"] and "docker" in report["missing"]


# --- the verifier ----------------------------------------------------------


class FakeRunner:
    """Stands in for the isolated Compose stack. Never starts anything."""

    def __init__(
        self, build: Any = None, error: str = "", portal_error: str = ""
    ) -> None:
        self.build = build or environment
        self.error = error
        self.portal_error = portal_error
        self.prepared: list[str] = []
        self.torn_down: list[str] = []
        self.served: list[str] = []

    def prepare(self, head_sha: str) -> Environment:
        self.prepared.append(head_sha)
        if self.error:
            raise RunnerError(self.error)
        return self.build()

    def teardown(self, env: Environment) -> None:
        self.torn_down.append(env.project)

    def serve_portal(self, env: Environment) -> Portal:
        self.served.append(env.project)
        if self.portal_error:
            raise RunnerError(self.portal_error)
        return Portal(
            url="http://127.0.0.1:8390/settings",
            project=env.project,
            data_dir=f"/tmp/{env.project}-portal-state",
            cleanup_command=f"docker compose --project-name {env.project} down",
        )


def case_result(case: str, broken: dict[str, Any] | None = None) -> dict[str, Any]:
    """A case result carrying exactly the assertions registered for it."""
    broken = broken or {}
    checks = [
        {
            "name": name,
            "kind": "control" if name.startswith("control_") else "target",
            "expected": True,
            "observed": broken.get(name, True),
            "holds": name not in broken,
        }
        for name in REQUIRED_CHECKS[case]
    ]
    return {
        "case": case,
        "verdict": FAILED if broken else PASSED,
        "checks": checks,
    }


def report(
    verdict: str,
    failures: tuple[str, ...] = (),
    cases: int = 2,
    broken: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if verdict == FAILED and not broken:
        broken = {REQUIRED_CHECKS["S2"][0]: "reused"}
    return {
        "verdict": verdict,
        "failures": list(failures),
        "cases": [case_result("S2", broken), case_result("N1")][:cases],
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
    outcome = verifier_for(
        wiring,
        store,
        replay=lambda env, cases: report(
            FAILED, broken={"new_exploration_does_not_reuse_a_discarded_key": True}
        ),
    ).verify(candidate, incident)

    assert outcome.verdict == FAILED
    # The lines come from the checks, not from the report's own summary.
    assert outcome.failures == (
        "S2.new_exploration_does_not_reuse_a_discarded_key: "
        "expected True, observed True",
    )


def test_a_pass_without_an_executed_assertion_is_blocked(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any], store: VerificationStore
) -> None:
    empty = {"verdict": PASSED, "cases": [], "failures": []}
    outcome = verifier_for(wiring, store, replay=lambda env, cases: empty).verify(
        candidate, incident
    )
    assert outcome.verdict == BLOCKED
    assert "no case results" in " ".join(outcome.failures)


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
    assert outcome.verdict == BLOCKED and "no case results" in " ".join(outcome.failures)


# --- the report cannot grade itself ----------------------------------------


@pytest.mark.parametrize(
    ("description", "cases", "expected"),
    [
        (
            "a case nobody asked for",
            [{"case": "WRONG_CASE", "verdict": PASSED,
              "checks": [{"name": "unregistered_check", "kind": "control", "holds": False}]}],
            "not requested",
        ),
        (
            "the same case twice",
            [case_result("S2"), case_result("S2"), case_result("N1")],
            "more than once",
        ),
        (
            "an assertion that is not registered",
            [{"case": "S2", "verdict": PASSED,
              "checks": [{"name": "looks_fine_to_me", "kind": "target", "holds": True}]},
             case_result("N1")],
            "is not a registered assertion",
        ),
        (
            "a registered assertion that never ran",
            [{"case": "S2", "verdict": PASSED,
              "checks": [c for c in case_result("S2")["checks"][1:]]},
             case_result("N1")],
            "never executed",
        ),
        (
            "an assertion with no outcome",
            [{"case": "S2", "verdict": PASSED,
              "checks": [{**c, "holds": "yes"} for c in case_result("S2")["checks"]]},
             case_result("N1")],
            "did not record whether it held",
        ),
        (
            "an assertion with no usable kind",
            [{"case": "S2", "verdict": PASSED,
              "checks": [{**c, "kind": "vibes"} for c in case_result("S2")["checks"]]},
             case_result("N1")],
            "has no usable kind",
        ),
        ("a case that was never reported", [case_result("S2")], "never ran"),
    ],
)
def test_a_malformed_report_cannot_pass_verification(
    wiring: Wiring,
    candidate: dict[str, Any],
    incident: dict[str, Any],
    store: VerificationStore,
    description: str,
    cases: list[dict[str, Any]],
    expected: str,
) -> None:
    forged = {"verdict": PASSED, "failures": [], "cases": cases}
    outcome = verifier_for(wiring, store, replay=lambda env, c: forged).verify(
        candidate, incident
    )
    assert outcome.verdict == BLOCKED, description
    assert expected in " ".join(outcome.failures), description


def test_a_failure_report_without_evidence_cannot_ask_for_a_fix(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any],
    store: VerificationStore, repairs: RepairStore,
) -> None:
    """An empty FAILED report must not buy a paid follow-up."""
    empty = {"verdict": FAILED, "failures": ["S2 is still broken"], "cases": []}
    verifying(wiring, store, replay=lambda env, cases: empty)
    assert wiring.controller.verify(int(candidate["id"])).action == "blocked"
    assert not wiring.devin_api.messages


def test_a_summary_that_disagrees_with_its_own_checks_is_blocked(
    wiring: Wiring, candidate: dict[str, Any], incident: dict[str, Any],
    store: VerificationStore,
) -> None:
    claims_pass = report(PASSED, broken={"an_omitted_row_limit_keeps_the_saved_value": 1000})
    claims_pass["cases"] = [case_result("S2", {"discarded_link_stays_dead_after_a_new_exploration": 200}),
                            case_result("N1")]
    outcome = verifier_for(wiring, store, replay=lambda env, cases: claims_pass).verify(
        candidate, incident
    )
    assert outcome.verdict == BLOCKED
    assert "reported passed while its assertions show failed" in " ".join(outcome.failures)


def test_the_registered_assertions_are_the_ones_the_real_validator_runs() -> None:
    """The registry is graded against a real baseline run, not against itself."""
    baseline = json.loads(
        (Path(__file__).resolve().parents[1]
         / "artifacts/phase5/negative-control/baseline.json").read_text(encoding="utf-8")
    )
    verdict, failures = grade(baseline, ("S2", "S1", "N1"))
    assert verdict == baseline["verdict"] == FAILED
    # Derived independently of the report's own summary, and identical to it.
    assert list(failures) == baseline["failures"]
    assert [f.split(":")[0] for f in failures] == [
        "S2.new_exploration_does_not_reuse_a_discarded_key",
        "S2.discarded_link_stays_dead_after_a_new_exploration",
        "S1.an_omitted_row_limit_keeps_the_saved_value",
    ]


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
    verifier.replay = lambda env, cases: report(FAILED)
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
    verifying(
        wiring,
        store,
        replay=lambda env, cases: report(
            FAILED, broken={"discarded_link_stays_dead_after_a_new_exploration": 200}
        ),
    )
    repair_id = int(candidate["id"])

    assert wiring.controller.verify(repair_id).action == "followed_up"
    repairs.update(repair_id, state=CANDIDATE)
    # Polling the same commit again must not send the same message twice.
    assert wiring.controller.verify(repair_id).action == "skipped"

    assert len(wiring.devin_api.messages) == 1
    session_id, message = wiring.devin_api.messages[0]
    assert session_id == wiring.session_id()
    assert "discarded_link_stays_dead_after_a_new_exploration" in message
    assert "observed 200" in message


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
