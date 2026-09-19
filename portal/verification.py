"""Independent verification of a candidate pull request.

The agent says it fixed the defect. This decides whether that is true, and it
is built so the agent cannot influence the answer:

* the pull request is re-read from GitHub — host, repository, head repository,
  base branch, state and the full 40-character head SHA — and never trusted
  from the session's own report;
* the changed paths are judged against a registered scope *before* anything is
  built, so a candidate that edits the validator, the fixtures, authentication
  or the CI configuration is rejected rather than executed;
* the candidate runs in its own Compose project with its own ports, database,
  cache and volumes, bind-mounted from its own checkout, with no controller,
  GitHub or Devin credential anywhere in its environment or mounts;
* the assertions come from :mod:`portal.validator` at a pinned automation
  revision, and the measured provenance of the running stack has to agree with
  the SHA under test;
* the head is re-read afterwards, so a newer commit cannot inherit an older
  pass.

Anything that leaves the question unanswered — setup failure, a service that
never came up, empty evidence, a moved head — is ``blocked``. Blocked is not a
pass and it is not a product failure: it asks for attention instead of telling
a session to change product code.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .events import utcnow
from .providers import GitHub
from .validator import BLOCKED, CASES_BY_FAMILY, FAILED, PASSED

#: Where a repair is allowed to change code. A repair for one runtime defect
#: touches product code and its tests; nothing else.
ALLOWED_PREFIXES: tuple[str, ...] = ("superset/", "tests/")

#: Checked first. Some of these are unreachable from the product repository
#: anyway (the validator lives in the automation repository); they are listed
#: so the policy states the whole rule in one place rather than relying on
#: repository layout to enforce it.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    ".github/",
    "docker/",
    "docker-compose",
    "Dockerfile",
    "requirements/",
    "scripts/",
    "stack/",
    "portal/",
    "scenarios/",
    "clients/",
    "helm/",
    "superset-frontend/",
    "superset/examples/",
    "superset/security/",
    "superset/migrations/",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "package-lock.json",
    "uv.lock",
    "RELEASING/",
)

MAX_CHANGED_FILES = 40


@dataclass(frozen=True)
class ScopeVerdict:
    allowed: bool
    reasons: tuple[str, ...] = ()


def check_scope(paths: list[str]) -> ScopeVerdict:
    """Whether this diff is a repair of one product defect, and nothing else."""
    if not paths:
        return ScopeVerdict(False, ("the pull request changes no files",))
    if len(paths) > MAX_CHANGED_FILES:
        return ScopeVerdict(
            False, (f"{len(paths)} files changed; a single-defect repair is smaller",)
        )
    reasons: list[str] = []
    for path in paths:
        if any(path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            reasons.append(f"{path} is outside what a repair may change")
        elif not any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES):
            reasons.append(f"{path} is not product code or a product test")
    return ScopeVerdict(not reasons, tuple(reasons))


# ------------------------------------------------------------------ running
class RunnerError(RuntimeError):
    """The candidate environment could not be produced. Always `blocked`."""


@dataclass
class Environment:
    """A started candidate stack and what was measured about it."""

    project: str
    base_url: str
    mcp_url: str
    checkout: str
    head_sha: str
    provenance: dict[str, Any] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)


class Runner(Protocol):
    """Builds, starts and tears down an isolated stack at one commit."""

    def prepare(self, head_sha: str) -> Environment: ...

    def teardown(self, environment: Environment) -> None: ...


def provenance_problem(environment: Environment, head_sha: str) -> str:
    """Why the measured stack cannot stand for this commit, or ``""``.

    The point of the check: a provenance file that merely *says* a SHA proves
    nothing. The recorded source path has to be the candidate checkout, the
    tree has to be clean, and the SHA the containers mounted has to be the one
    under verification.
    """
    measured = environment.provenance or {}
    if not measured:
        return "no provenance was measured for the candidate stack"
    if not measured.get("measured_at"):
        return "the candidate provenance carries no measurement time"
    for service in ("web", "mcp"):
        service_data = measured.get(service) or {}
        if not service_data:
            return f"nothing was measured for the {service} service"
        if str(service_data.get("source_sha") or "").lower() != head_sha.lower():
            return (
                f"the {service} service is running "
                f"{service_data.get('source_sha') or 'an unknown commit'}, not {head_sha}"
            )
        mount = str(service_data.get("source_mount") or "")
        if not mount or not mount.startswith(environment.checkout):
            return (
                f"the {service} service mounts {mount or 'an unknown path'}, "
                f"not the candidate checkout {environment.checkout}"
            )
        if service_data.get("clean") is False:
            return f"the {service} checkout has uncommitted changes"
    return ""


# ------------------------------------------------------------------ records
SCHEMA = """
CREATE TABLE IF NOT EXISTS verifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    repair_id      INTEGER NOT NULL,
    incident_id    INTEGER NOT NULL,
    simulated      INTEGER NOT NULL DEFAULT 0,
    candidate_sha  TEXT NOT NULL,
    pr_url         TEXT NOT NULL,
    validator_ref  TEXT NOT NULL,
    fixture_rev    TEXT,
    config_rev     TEXT,
    cases          TEXT NOT NULL,
    verdict        TEXT NOT NULL,
    reason         TEXT,
    failures       TEXT,
    commands       TEXT,
    provenance     TEXT,
    report         TEXT,
    artifact_path  TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT NOT NULL
);
"""


class VerificationStore:
    """Every attempt, kept whatever the outcome was."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._conn:
            self._conn.executescript(SCHEMA)

    #: The only columns an attempt may write. The insert builds its column
    #: list from the caller's keys, so the list is checked against this rather
    #: than interpolated on trust.
    COLUMNS = frozenset(
        {
            "repair_id", "incident_id", "simulated", "candidate_sha", "pr_url",
            "validator_ref", "fixture_rev", "config_rev", "cases", "verdict",
            "reason", "failures", "commands", "provenance", "report",
            "artifact_path", "started_at", "finished_at",
        }
    )

    def record(self, attempt: dict[str, Any]) -> int:
        unknown = set(attempt) - self.COLUMNS
        if unknown:
            raise ValueError(f"unknown verification column(s): {sorted(unknown)}")
        columns = ", ".join(attempt)
        marks = ", ".join("?" for _ in attempt)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"INSERT INTO verifications ({columns}) VALUES ({marks})",
                tuple(attempt.values()),
            )
        return int(cursor.lastrowid or 0)

    def for_repair(self, repair_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT * FROM verifications WHERE repair_id = ? ORDER BY id",
                (repair_id,),
            ).fetchall()
        ]

    def for_incident(self, incident_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT * FROM verifications WHERE incident_id = ? ORDER BY id",
                (incident_id,),
            ).fetchall()
        ]

    def verified_count(self) -> int:
        """Real passes only. A simulated lifecycle never counts as one."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM verifications "
            "WHERE verdict = ? AND simulated = 0",
            (PASSED,),
        ).fetchone()
        return int(row["n"] if row else 0)


# ----------------------------------------------------------------- verifier
@dataclass
class Outcome:
    verdict: str
    reason: str = ""
    failures: tuple[str, ...] = ()
    record_id: int = 0
    candidate_sha: str = ""


class Verifier:
    """Reads the candidate, runs it in isolation, and records what happened."""

    def __init__(
        self,
        *,
        github: GitHub,
        runner: Runner,
        store: VerificationStore,
        target_repo: str,
        base_branch: str,
        validator_ref: str,
        replay: Any,
        artifacts: Path | None = None,
        simulated: bool = False,
    ) -> None:
        self.github = github
        self.runner = runner
        self.store = store
        self.target_repo = target_repo
        self.base_branch = base_branch
        #: The automation revision the assertions come from. It is pinned by
        #: the deployment, not by the candidate.
        self.validator_ref = validator_ref
        #: `replay(environment, cases) -> report dict`. Injected so the same
        #: verifier drives a real stack, a baseline negative control, or a
        #: labelled simulation.
        self.replay = replay
        self.artifacts = artifacts
        self.simulated = simulated

    def cases_for(self, family: str) -> tuple[str, ...]:
        """One repair, one defect: the other known baseline defect is not required."""
        return CASES_BY_FAMILY.get(family, ())

    def verify(self, repair: dict[str, Any], incident: dict[str, Any]) -> Outcome:
        started = utcnow()
        pr_url = str(repair.get("agent_pr_url") or "")
        cases = self.cases_for(str(incident.get("family") or ""))
        environment: Environment | None = None
        report: dict[str, Any] = {}
        commands: list[str] = []
        head_sha = ""
        try:
            head_sha, files = self._read_candidate(pr_url)
            scope = check_scope(files)
            if not scope.allowed:
                # A diff outside the registered scope is a policy stop, not
                # evidence about the product: nothing was executed, so there
                # is nothing to tell the session about its fix. It asks for
                # attention instead.
                return self._finish(
                    repair, incident, started, BLOCKED, head_sha, pr_url, cases,
                    reason="the candidate changes files a repair may not change",
                    failures=scope.reasons, report={"changed_files": files},
                )
            if not cases:
                return self._finish(
                    repair, incident, started, BLOCKED, head_sha, pr_url, cases,
                    reason="no registered case answers this failure family",
                )
            environment = self.runner.prepare(head_sha)
            commands = list(environment.commands)
            problem = provenance_problem(environment, head_sha)
            if problem:
                return self._finish(
                    repair, incident, started, BLOCKED, head_sha, pr_url, cases,
                    reason=problem, commands=commands,
                    provenance=environment.provenance,
                )
            report = self.replay(environment, cases)
            moved = self._moved(pr_url, head_sha)
            if moved:
                return self._finish(
                    repair, incident, started, BLOCKED, head_sha, pr_url, cases,
                    reason=moved, commands=commands, report=report,
                    provenance=environment.provenance,
                )
        except (RunnerError, RuntimeError, ValueError, OSError) as exc:
            return self._finish(
                repair, incident, started, BLOCKED, head_sha, pr_url, cases,
                reason=f"{type(exc).__name__}: {exc}", commands=commands,
                report=report,
                provenance=environment.provenance if environment else {},
            )
        finally:
            if environment is not None:
                try:
                    self.runner.teardown(environment)
                except (RunnerError, OSError):
                    pass

        verdict = str(report.get("verdict") or BLOCKED)
        failures = tuple(report.get("failures") or ())
        if verdict not in (PASSED, FAILED, BLOCKED):
            verdict, failures = BLOCKED, (f"unreadable verdict {verdict!r}",)
        if verdict == PASSED and not self._has_evidence(report, cases):
            verdict = BLOCKED
            failures = ("the replay produced no executed assertion",)
        return self._finish(
            repair, incident, started, verdict, head_sha, pr_url, cases,
            reason="" if verdict == PASSED else "; ".join(failures[:3]),
            failures=failures, commands=commands, report=report,
            provenance=environment.provenance if environment else {},
        )

    # --- pieces ------------------------------------------------------------
    def _read_candidate(self, pr_url: str) -> tuple[str, list[str]]:
        number = _number_of(pr_url, self.target_repo)
        head = self.github.pull_request_head(number)
        problem = self._head_problem(head)
        if problem:
            raise RuntimeError(problem)
        return head["head_sha"], self.github.pull_request_files(number)

    def _head_problem(self, head: dict[str, str]) -> str:
        if head.get("base_ref") != self.base_branch:
            return f"targets {head.get('base_ref') or 'an unknown branch'}, not {self.base_branch}"
        if head.get("head_repo") != self.target_repo:
            return (
                f"the head branch lives in {head.get('head_repo') or 'an unknown repository'}, "
                f"not {self.target_repo}"
            )
        sha = str(head.get("head_sha") or "").lower()
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            return "the head SHA is not a full commit id"
        if head.get("merged") == "true":
            return "the pull request is already merged"
        if head.get("state") != "open":
            return f"the pull request is {head.get('state') or 'in an unreported state'}"
        return ""

    def _moved(self, pr_url: str, head_sha: str) -> str:
        """A pass belongs to the commit that was tested, and to no other."""
        head = self.github.pull_request_head(_number_of(pr_url, self.target_repo))
        if head.get("head_sha", "").lower() != head_sha.lower():
            return (
                f"the candidate moved to {head.get('head_sha')} while "
                f"{head_sha} was being verified"
            )
        return self._head_problem(head)

    @staticmethod
    def _has_evidence(report: dict[str, Any], cases: tuple[str, ...]) -> bool:
        results = report.get("cases") or []
        if len(results) != len(cases):
            return False
        for result in results:
            checks = result.get("checks") or []
            if not any(check.get("kind") in ("target", "control") for check in checks):
                return False
        return True

    def _finish(
        self,
        repair: dict[str, Any],
        incident: dict[str, Any],
        started: str,
        verdict: str,
        head_sha: str,
        pr_url: str,
        cases: tuple[str, ...],
        *,
        reason: str = "",
        failures: tuple[str, ...] = (),
        commands: list[str] | None = None,
        report: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> Outcome:
        artifact = self._write_artifact(repair, head_sha, report or {})
        record_id = self.store.record(
            {
                "repair_id": int(repair["id"]),
                "incident_id": int(repair["incident_id"]),
                "simulated": int(self.simulated),
                "candidate_sha": head_sha,
                "pr_url": pr_url,
                "validator_ref": self.validator_ref,
                "fixture_rev": str(incident.get("fixture_revision") or ""),
                "config_rev": str(incident.get("config_revision") or ""),
                "cases": json.dumps(list(cases)),
                "verdict": verdict,
                "reason": reason,
                "failures": json.dumps(list(failures)),
                "commands": json.dumps(commands or []),
                "provenance": json.dumps(provenance or {}),
                "report": json.dumps(report or {}, default=str),
                "artifact_path": artifact,
                "started_at": started,
                "finished_at": utcnow(),
            }
        )
        return Outcome(verdict, reason, failures, record_id, head_sha)

    def _write_artifact(
        self, repair: dict[str, Any], head_sha: str, report: dict[str, Any]
    ) -> str:
        if self.artifacts is None or not report:
            return ""
        directory = self.artifacts / f"repair-{int(repair['id'])}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{head_sha[:12] or 'unknown'}-{utcnow().replace(':', '')}.json"
        path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        return str(path)


def _number_of(pr_url: str, repo: str) -> int:
    prefix = f"https://github.com/{repo}/pull/"
    if not pr_url.startswith(prefix):
        raise ValueError(f"{pr_url or 'an empty URL'} is not a pull request on {repo}")
    tail = pr_url[len(prefix):].split("/")[0].split("?")[0]
    if not tail.isdigit():
        raise ValueError(f"{pr_url} has no pull request number")
    return int(tail)


__all__ = [
    "ALLOWED_PREFIXES",
    "BLOCKED",
    "Environment",
    "FAILED",
    "FORBIDDEN_PREFIXES",
    "Outcome",
    "PASSED",
    "Runner",
    "RunnerError",
    "ScopeVerdict",
    "VerificationStore",
    "Verifier",
    "check_scope",
    "provenance_problem",
]
