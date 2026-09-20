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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .events import utcnow
from .providers import Commit, GitHub
from .validator import BLOCKED, CASES_BY_FAMILY, FAILED, PASSED, REQUIRED_CHECKS

GRADED_KINDS = ("target", "control")
KNOWN_KINDS = GRADED_KINDS + ("setup",)

#: Which question an attempt answered. `preview` grades the pull request's own
#: head before anybody merges it; `post_merge` grades the commit a human
#: actually merged, which is a different commit and a different claim.
PREVIEW, POST_MERGE = "preview", "post_merge"

#: Where a repair is allowed to change code, per failure family. A repair for
#: one runtime defect touches the product code that defect lives in and the
#: tests for it; ``superset/`` as a whole is far wider than one defect, and
#: includes files such as ``superset/config.py`` that set authentication and
#: CSRF for the entire application.
SCOPE_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "discarded_form_data_key_is_reused": (
        "superset/explore/form_data/",
        "superset/commands/explore/form_data/",
        "superset/key_value/",
        "superset/commands/key_value/",
        "tests/unit_tests/explore/",
        "tests/unit_tests/commands/explore/",
        "tests/unit_tests/key_value/",
        "tests/unit_tests/commands/key_value/",
        "tests/integration_tests/explore/",
    ),
    "omitted_row_limit_is_reset": (
        "superset/commands/chart/",
        "superset/charts/",
        "superset/mcp_service/",
        "tests/unit_tests/charts/",
        "tests/unit_tests/commands/chart/",
        "tests/unit_tests/mcp_service/",
        "tests/integration_tests/charts/",
    ),
    # A number-formatting defect lives in the frontend, so this family's
    # scope is the formatting package and the chart plugin that renders the
    # affected cells — not the frontend tree at large, which is where the
    # build, the authentication surface and every other plugin also live.
    "bigint_number_format_not_applied": (
        "superset-frontend/packages/superset-ui-core/src/number-format/",
        "superset-frontend/packages/superset-ui-core/test/number-format/",
        "superset-frontend/plugins/plugin-chart-table/src/",
        "superset-frontend/plugins/plugin-chart-table/test/",
    ),
}

#: Where the *product* lives, per family: a diff of tests alone repairs
#: nothing, and for a frontend defect `superset/` would be the wrong tree to
#: demand a change in.
PRODUCT_PREFIX = "superset/"
PRODUCT_PREFIX_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "bigint_number_format_not_applied": (
        "superset-frontend/packages/superset-ui-core/src/",
        "superset-frontend/plugins/plugin-chart-table/src/",
    ),
}

#: Checked first. Some of these are unreachable from the product repository
#: anyway (the validator lives in the automation repository); they are listed
#: so the policy states the whole rule in one place rather than relying on
#: repository layout to enforce it.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    ".github/",
    "superset/config.py",
    "superset/app.py",
    "superset/initialization/",
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


def check_scope(paths: list[str], family: str) -> ScopeVerdict:
    """Whether this diff repairs *this* defect, and nothing else.

    Path policy does not prove a change is semantically safe — only review and
    the replay can say that. What it does prove is that the diff stays inside
    the code the registered defect lives in, so global configuration, the
    fixtures, the grader or the build cannot ride along into a run whose
    verdict is about something else. An unregistered family has no scope and
    goes to review rather than defaulting to the whole product tree.
    """
    if not paths:
        return ScopeVerdict(False, ("the pull request changes no files",))
    if len(paths) > MAX_CHANGED_FILES:
        return ScopeVerdict(
            False, (f"{len(paths)} files changed; a single-defect repair is smaller",)
        )
    allowed = SCOPE_BY_FAMILY.get(family)
    forbidden = _forbidden_for(allowed or ())
    product = PRODUCT_PREFIX_BY_FAMILY.get(family, (PRODUCT_PREFIX,))
    if not allowed:
        return ScopeVerdict(
            False,
            (
                f"no change scope is registered for {family or 'an unnamed family'}; "
                "this candidate needs review",
            ),
        )
    reasons: list[str] = []
    for path in paths:
        if any(path.startswith(prefix) for prefix in forbidden):
            reasons.append(f"{path} is outside what a repair may change")
        elif not any(path.startswith(prefix) for prefix in allowed):
            reasons.append(f"{path} is not in the registered scope for {family}")
    if not reasons and not any(
        path.startswith(prefix) for path in paths for prefix in product
    ):
        reasons.append("the candidate changes no product code")
    return ScopeVerdict(not reasons, tuple(reasons))


def _forbidden_for(allowed: tuple[str, ...]) -> tuple[str, ...]:
    """The blanket bans, minus those a family's own scope sits inside.

    `superset-frontend/` is banned by default because it carries the build
    and every other plugin, but a frontend defect cannot be repaired without
    touching it. Dropping only the bans a registered scope is nested in keeps
    the narrow path list as the thing that decides, rather than widening the
    policy for every family at once.
    """
    return tuple(
        prefix
        for prefix in FORBIDDEN_PREFIXES
        if not any(path.startswith(prefix) for path in allowed)
    )


# ------------------------------------------------------------------ grading
def graded_checks(report: dict[str, Any]) -> int:
    """How many graded assertions a report actually carries.

    Counted from the assertions themselves so that anything quoting a number
    is quoting the evidence rather than a summary line.
    """
    total = 0
    for result in report.get("cases") or []:
        if not isinstance(result, dict):
            continue
        for check in result.get("checks") or []:
            if isinstance(check, dict) and check.get("kind") in GRADED_KINDS:
                total += 1
    return total


def grade(report: dict[str, Any], cases: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """The verdict the *checks* support, ignoring what the report claims.

    A replay hands back a summary and a list of results. The summary is the
    easiest thing in the system to get wrong and the easiest to forge, so it
    is not consulted: the verdict is rebuilt from the graded assertions and
    the report only has to agree.

    Every requested case must appear exactly once, carrying exactly the
    assertions registered for it in :data:`portal.validator.REQUIRED_CHECKS`,
    each with a usable kind and a boolean outcome. A missing, duplicated,
    renamed or foreign assertion means the question was not answered:
    ``blocked``, not ``failed`` — there is no product evidence to send back,
    and a session asked to fix something on that basis would be paid to chase
    nothing.
    """
    results = report.get("cases")
    if not isinstance(results, list) or not results:
        return BLOCKED, ("the replay produced no case results",)

    seen: list[str] = []
    failures: list[str] = []
    blocked: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            return BLOCKED, ("a case result is not a record",)
        name = str(result.get("case") or "")
        if name in seen:
            return BLOCKED, (f"case {name} was reported more than once",)
        seen.append(name)
        if name not in cases:
            return BLOCKED, (
                f"the replay reported case {name or '<unnamed>'}, which was not requested",
            )
        if str(result.get("verdict") or "") == BLOCKED or result.get("blocked_reason"):
            blocked.append(
                f"{name}: blocked — {result.get('blocked_reason') or 'no reason given'}"
            )
            continue
        problem, case_failures = _grade_case(name, result)
        if problem:
            return BLOCKED, (problem,)
        failures.extend(case_failures)

    missing = [case for case in cases if case not in seen]
    if missing:
        return BLOCKED, tuple(f"case {case} never ran" for case in missing)
    if blocked:
        return BLOCKED, tuple(blocked)
    if failures:
        return FAILED, tuple(failures)
    return PASSED, ()


def _grade_case(name: str, result: dict[str, Any]) -> tuple[str, list[str]]:
    """``(problem, failure lines)`` for one case that claims to have run."""
    required = REQUIRED_CHECKS.get(name)
    if not required:
        return f"no registered assertions exist for case {name}", []
    checks = result.get("checks")
    if not isinstance(checks, list):
        return f"case {name} reported no assertions", []

    graded: dict[str, bool] = {}
    for check in checks:
        if not isinstance(check, dict):
            return f"case {name} reported an assertion that is not a record", []
        check_name = str(check.get("name") or "")
        kind = str(check.get("kind") or "")
        if kind not in KNOWN_KINDS:
            return f"{name}.{check_name or '<unnamed>'} has no usable kind", []
        if kind not in GRADED_KINDS:
            continue
        if check_name not in required:
            return f"{name}.{check_name or '<unnamed>'} is not a registered assertion", []
        if check_name in graded:
            return f"{name}.{check_name} was reported twice", []
        holds = check.get("holds")
        if not isinstance(holds, bool):
            return f"{name}.{check_name} did not record whether it held", []
        graded[check_name] = holds

    absent = [check for check in required if check not in graded]
    if absent:
        return f"case {name} never executed {', '.join(absent)}", []

    return "", [
        f"{name}.{check['name']}: expected {check.get('expected')!r}, "
        f"observed {check.get('observed')!r}"
        + (f" ({check['note']})" if check.get("note") else "")
        for check in checks
        if isinstance(check, dict)
        and check.get("kind") in GRADED_KINDS
        and check.get("holds") is False
    ]


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
    #: Identifiers a browser case needs, produced by this stack's own
    #: fixture run: the saved charts belong to this metadata database and
    #: no other, so they travel with the environment rather than a config.
    fixture: dict[str, int] = field(default_factory=dict)


@dataclass
class Portal:
    """A retained portal somebody can open, and how to take it down."""

    url: str
    project: str
    data_dir: str
    cleanup_command: str


def cleanup_command(environment: Environment) -> str:
    """How an operator takes a retained stack down again, by hand.

    A retained demo outlives the run that produced it, so the command that
    removes it belongs in the evidence rather than in somebody's memory.
    """
    return (
        f"docker compose --project-name {environment.project} "
        f"--project-directory {environment.checkout} "
        f"-f {environment.checkout}/docker-compose-light.yml "
        "down -v --remove-orphans"
    )


class Runner(Protocol):
    """Builds, starts and tears down an isolated stack at one commit."""

    def prepare(self, head_sha: str, family: str = "") -> Environment: ...

    def teardown(self, environment: Environment) -> None: ...

    def serve_portal(self, environment: Environment) -> Portal: ...


#: How old a measurement may be and still describe the run it belongs to.
MAX_PROVENANCE_AGE_MINUTES = 360


def _inside(mount: str, checkout: str) -> bool:
    """Whether `mount` is the checkout or lives under it, by resolved path.

    String containment is not enough: ``/tmp/candidate-other/superset`` starts
    with ``/tmp/candidate`` and is a different tree.
    """
    try:
        resolved = Path(mount).resolve()
        root = Path(checkout).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved == root or root in resolved.parents


#: Families whose verdict depends on compiled assets. For these, a stack that
#: serves a bundle somebody else built answers a different question, however
#: correct its Python source is.
ASSET_BUILT_FAMILIES = ("bigint_number_format_not_applied",)


def _asset_problem(measured: dict[str, Any], head_sha: str) -> str:
    """Why the served frontend bundle cannot stand for this commit, or ``""``."""
    assets = measured.get("assets") or {}
    if not assets:
        return "no frontend bundle was built for a candidate whose defect is in the frontend"
    if str(assets.get("built_from_sha") or "").lower() != head_sha.lower():
        return (
            f"the served frontend bundle was built from "
            f"{assets.get('built_from_sha') or 'an unknown commit'}, not {head_sha}"
        )
    if not assets.get("files") or not assets.get("bundle_hash"):
        return "the frontend bundle was not measured, so it cannot be told from a stale one"
    return ""


def provenance_problem(
    environment: Environment,
    head_sha: str,
    *,
    family: str = "",
    now: datetime | None = None,
) -> str:
    """Why the measured stack cannot stand for this commit, or ``""``.

    A provenance file that merely *says* a SHA proves nothing, so nothing here
    is optional and nothing absent counts as true. Each service must name the
    container and image that served it, be running, mount a path that resolves
    inside the candidate checkout, sit on the commit under verification with a
    clean tree, and — the part a stale or mis-built image fails — hold source
    whose digest, measured *inside* that container, equals the digest of the
    tree the verifier itself checked out.
    """
    measured = environment.provenance or {}
    if not measured:
        return "no provenance was measured for the candidate stack"

    stamp = str(measured.get("measured_at") or "")
    try:
        measured_at = datetime.fromisoformat(stamp)
    except ValueError:
        return f"the candidate provenance carries no readable measurement time ({stamp!r})"
    if measured_at.tzinfo is None:
        measured_at = measured_at.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    age = (reference - measured_at).total_seconds() / 60
    if age > MAX_PROVENANCE_AGE_MINUTES:
        return f"the candidate provenance was measured {int(age)} minutes ago"
    if age < -5:
        return "the candidate provenance is stamped in the future"

    if str(measured.get("compose_project") or "") != environment.project:
        return (
            f"the provenance describes Compose project "
            f"{measured.get('compose_project') or 'nothing'}, not {environment.project}"
        )

    checkout = measured.get("checkout") or {}
    expected_hash = str(checkout.get("code_hash") or "")
    if not expected_hash:
        return "the candidate checkout was never hashed, so no container can be compared to it"
    if not _inside(str(checkout.get("path") or ""), environment.checkout):
        return (
            f"the hashed tree {checkout.get('path') or 'an unknown path'} is not the "
            f"candidate checkout {environment.checkout}"
        )
    if str(checkout.get("source_sha") or "").lower() != head_sha.lower():
        return (
            f"the candidate checkout is at "
            f"{checkout.get('source_sha') or 'an unknown commit'}, not {head_sha}"
        )
    if checkout.get("clean") is not True:
        return "the candidate checkout is not a clean tree"

    for service in ("web", "mcp"):
        problem = _service_problem(
            service, measured.get(service) or {}, environment, head_sha, expected_hash
        )
        if problem:
            return problem

    fixture = measured.get("fixture") or {}
    if fixture.get("error") or not fixture.get("digest"):
        return (
            "the fixture the run would read was not measured"
            + (f": {fixture['error']}" if fixture.get("error") else "")
        )
    if not measured.get("automation_ref"):
        return "the provenance does not say which automation revision graded the run"
    if not measured.get("config_revision"):
        return "the provenance does not say which configuration the stack was started with"
    if family in ASSET_BUILT_FAMILIES:
        return _asset_problem(measured, head_sha)
    return ""


def _service_problem(
    service: str,
    data: dict[str, Any],
    environment: Environment,
    head_sha: str,
    expected_hash: str,
) -> str:
    """Why one measured service cannot stand for the candidate commit."""
    if not data or data.get("error"):
        return (
            f"nothing usable was measured for the {service} service"
            + (f": {data['error']}" if data.get("error") else "")
        )
    for field_name in ("container_id", "image_id"):
        if not data.get(field_name):
            return f"the {service} service reports no {field_name.replace('_', ' ')}"
    if str(data.get("state") or "") != "running":
        return f"the {service} container is {data.get('state') or 'in an unreported state'}"
    if str(data.get("health") or "none") == "unhealthy":
        return f"the {service} container is unhealthy"
    mount = str(data.get("source_mount") or "")
    if not mount or not _inside(mount, environment.checkout):
        return (
            f"the {service} service mounts {mount or 'an unknown path'}, "
            f"not the candidate checkout {environment.checkout}"
        )
    if str(data.get("source_sha") or "").lower() != head_sha.lower():
        return (
            f"the {service} service is running "
            f"{data.get('source_sha') or 'an unknown commit'}, not {head_sha}"
        )
    if data.get("clean") is not True:
        return f"the {service} checkout is not a clean tree"
    if str(data.get("code_hash") or "") != expected_hash:
        return (
            f"the source inside the {service} container hashes to "
            f"{data.get('code_hash') or 'nothing'}, not {expected_hash}"
        )
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
    stage          TEXT NOT NULL DEFAULT 'preview',
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
            # A store written before the merge gate existed has every column
            # but this one, and its attempts all graded a pull request head.
            existing = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(verifications)")
            }
            if "stage" not in existing:
                self._conn.execute(
                    "ALTER TABLE verifications ADD COLUMN stage TEXT NOT NULL "
                    f"DEFAULT '{PREVIEW}'"
                )

    #: The only columns an attempt may write. The insert builds its column
    #: list from the caller's keys, so the list is checked against this rather
    #: than interpolated on trust.
    COLUMNS = frozenset(
        {
            "repair_id", "incident_id", "simulated", "candidate_sha", "pr_url",
            "validator_ref", "fixture_rev", "config_rev", "cases", "verdict",
            "reason", "failures", "commands", "provenance", "report",
            "artifact_path", "stage", "started_at", "finished_at",
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

    def get(self, verification_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM verifications WHERE id = ?", (verification_id,)
        ).fetchone()
        return dict(row) if row else None

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
    stage: str = PREVIEW
    checks: int = 0


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
        retain_merged: bool = False,
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
        #: Whether a passing post-merge stack stays up on loopback. The demo
        #: has to show the merged code running somewhere; a preview stack is
        #: torn down the moment it has answered.
        self.retain_merged = retain_merged

    def cases_for(self, family: str) -> tuple[str, ...]:
        """One repair, one defect: the other known baseline defect is not required."""
        return CASES_BY_FAMILY.get(family, ())

    def verify(
        self,
        repair: dict[str, Any],
        incident: dict[str, Any],
        *,
        merge_sha: str = "",
    ) -> Outcome:
        """Grade the pull request's own head, or the commit a human merged.

        With `merge_sha` the question changes: not whether this candidate
        would work, but whether the code that exists after the merge does.
        The commit is taken from GitHub's merge record, the stack is built
        from it, and the same registered cases decide. A preview pass is
        never renamed into a post-merge one.
        """
        started = utcnow()
        stage = POST_MERGE if merge_sha else PREVIEW
        pr_url = str(repair.get("agent_pr_url") or "")
        family = str(incident.get("family") or "")
        cases = self.cases_for(family)
        environment: Environment | None = None
        report: dict[str, Any] = {}
        commands: list[str] = []
        head_sha = ""
        keep = False
        verdict: str = BLOCKED
        failures: tuple[str, ...] = ()
        try:
            if merge_sha:
                head_sha, files = self._read_merged(
                    pr_url, merge_sha, str(repair.get("pr_head_sha") or "")
                )
            else:
                head_sha, files = self._read_candidate(pr_url)
            scope = check_scope(files, family)
            if not scope.allowed:
                # A diff outside the registered scope is a policy stop, not
                # evidence about the product: nothing was executed, so there
                # is nothing to tell the session about its fix. It asks for
                # attention instead.
                return self._finish(
                    repair, incident, started, BLOCKED, head_sha, pr_url, cases,
                    reason="the candidate changes files a repair may not change",
                    failures=scope.reasons, report={"changed_files": files},
                    stage=stage,
                )
            if not cases:
                return self._finish(
                    repair, incident, started, BLOCKED, head_sha, pr_url, cases,
                    reason="no registered case answers this failure family",
                    stage=stage,
                )
            environment = self.runner.prepare(head_sha, family)
            commands = list(environment.commands)
            problem = provenance_problem(environment, head_sha, family=family)
            if problem:
                return self._finish(
                    repair, incident, started, BLOCKED, head_sha, pr_url, cases,
                    reason=problem, commands=commands,
                    provenance=environment.provenance, stage=stage,
                )
            report = self.replay(environment, cases)
            moved = (
                self._merge_moved(
                    pr_url, merge_sha, str(repair.get("pr_head_sha") or "")
                )
                if merge_sha
                else self._moved(pr_url, head_sha)
            )
            if moved:
                return self._finish(
                    repair, incident, started, BLOCKED, head_sha, pr_url, cases,
                    reason=moved, commands=commands, report=report,
                    provenance=environment.provenance, stage=stage,
                )
            verdict, failures = grade(report, cases)
            claimed = str(report.get("verdict") or "")
            if verdict != BLOCKED and claimed != verdict:
                # Whichever of the two is wrong, this run cannot promote a
                # candidate or bill a session for a fix.
                failures = (
                    f"the replay reported {claimed or 'no verdict'} while its "
                    f"assertions show {verdict}",
                )
                verdict = BLOCKED
            if self.retain_merged and stage == POST_MERGE and verdict == PASSED:
                keep = True
                report["retained_environment"] = self._retain(environment)
        except (RunnerError, RuntimeError, ValueError, OSError) as exc:
            return self._finish(
                repair, incident, started, BLOCKED, head_sha, pr_url, cases,
                reason=f"{type(exc).__name__}: {exc}", commands=commands,
                report=report,
                provenance=environment.provenance if environment else {},
                stage=stage,
            )
        finally:
            if environment is not None and not keep:
                try:
                    self.runner.teardown(environment)
                except (RunnerError, OSError):
                    pass

        return self._finish(
            repair, incident, started, verdict, head_sha, pr_url, cases,
            reason="" if verdict == PASSED else "; ".join(failures[:3]),
            failures=failures, commands=commands, report=report,
            provenance=environment.provenance if environment else {},
            stage=stage,
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

    def _read_merged(
        self, pr_url: str, merge_sha: str, expected_head: str
    ) -> tuple[str, list[str]]:
        """The merged commit, as GitHub records it rather than as anyone claims it."""
        number = _number_of(pr_url, self.target_repo)
        head = self.github.pull_request_head(number)
        problem = self._merged_problem(head, merge_sha, expected_head)
        if problem:
            raise RuntimeError(problem)
        sha = merge_sha.strip().lower()
        merged, previewed = self.github.commit(sha), self.github.commit(expected_head)
        content = self._content_problem(merged, previewed)
        if content:
            raise RuntimeError(content)
        # What landed is measured against the commit it landed on, not
        # against the pull request's own branch: a change the merge itself
        # introduced appears in no list the pull request keeps.
        return sha, self.github.compare_files(merged.parents[0], sha)

    def _content_problem(self, merged: Commit, previewed: Commit) -> str:
        """Why the merged content is not the previewed content, if it is not.

        Tree ids answer this independently of how the merge was performed:
        a merge commit, a squash and a rebase that carry the reviewed content
        all name the same tree, and anything else — an edit during the merge,
        a base that moved under it — names a different one.
        """
        if not previewed.tree_sha or not merged.tree_sha:
            return "the merged or previewed tree could not be read from Git"
        if not merged.parents:
            return f"the merge commit {merged.sha[:12]} has no parent to compare against"
        if merged.tree_sha != previewed.tree_sha:
            return (
                f"the merged tree {merged.tree_sha[:12]} is not the previewed tree "
                f"{previewed.tree_sha[:12]}"
            )
        return ""

    def _merged_problem(
        self, head: dict[str, str], merge_sha: str, expected_head: str
    ) -> str:
        """Why this merge is not the merge being graded, if it is not.

        A pull request that was closed rather than merged, merged somewhere
        else, or merged from a head nobody previewed is a different event
        from the one this run claims to measure.
        """
        if head.get("base_ref") != self.base_branch:
            return (
                f"the merge went into {head.get('base_ref') or 'an unknown branch'}, "
                f"not {self.base_branch}"
            )
        if head.get("head_repo") != self.target_repo:
            return (
                f"the head branch lives in {head.get('head_repo') or 'an unknown repository'}, "
                f"not {self.target_repo}"
            )
        if head.get("merged") != "true":
            return "the pull request is not merged"
        wanted = merge_sha.strip().lower()
        if len(wanted) != 40 or any(c not in "0123456789abcdef" for c in wanted):
            return "the merge commit is not a full commit id"
        actual = str(head.get("merge_commit_sha") or "").lower()
        if actual != wanted:
            return (
                f"the pull request reports merge commit {actual[:12] or 'none'}, "
                f"not {wanted[:12]}"
            )
        merged_head = str(head.get("head_sha") or "").lower()
        wanted_head = expected_head.strip().lower()
        if len(wanted_head) != 40 or len(merged_head) != 40:
            return (
                "the previewed candidate or the merged head is not a full commit id "
                f"(previewed {wanted_head[:12] or 'none'}, merged {merged_head[:12] or 'none'})"
            )
        if merged_head != wanted_head:
            return (
                f"the merged head is {merged_head[:12]}, not the "
                f"candidate {wanted_head[:12]} that was verified"
            )
        return ""

    def _merge_moved(self, pr_url: str, merge_sha: str, expected_head: str) -> str:
        """A post-merge pass belongs to the merge commit that was tested."""
        head = self.github.pull_request_head(_number_of(pr_url, self.target_repo))
        return self._merged_problem(head, merge_sha, expected_head)

    def _moved(self, pr_url: str, head_sha: str) -> str:
        """A pass belongs to the commit that was tested, and to no other."""
        head = self.github.pull_request_head(_number_of(pr_url, self.target_repo))
        if head.get("head_sha", "").lower() != head_sha.lower():
            return (
                f"the candidate moved to {head.get('head_sha')} while "
                f"{head_sha} was being verified"
            )
        return self._head_problem(head)

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
        stage: str = PREVIEW,
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
                "stage": stage,
                "started_at": started,
                "finished_at": utcnow(),
            }
        )
        return Outcome(
            verdict,
            reason,
            failures,
            record_id,
            head_sha,
            stage,
            graded_checks(report or {}),
        )

    def _retain(self, environment: Environment) -> dict[str, str]:
        """Keep the merged stack on loopback, and say where it is.

        A replay that is torn down the moment it passes leaves whoever comes
        to look at the demo in front of the old build. The record names the
        portal to open, the commit the running containers were measured at
        and the command that removes it again.
        """
        web = environment.provenance.get("web") or {}
        try:
            portal = self.runner.serve_portal(environment)
            portal_url, portal_cleanup, portal_state = (
                portal.url,
                portal.cleanup_command,
                "running",
            )
        except (RunnerError, OSError) as exc:
            # The backend is still retained and still the merged commit; only
            # the page is missing, and saying so is the whole point of the
            # record. A URL nobody can open would read as a working demo.
            portal_url, portal_cleanup = "", ""
            portal_state = f"not started: {type(exc).__name__}: {exc}"
        record = {
            "project": environment.project,
            "portal_url": portal_url,
            "portal_state": portal_state,
            "portal_cleanup_command": portal_cleanup,
            "base_url": environment.base_url,
            "checkout": environment.checkout,
            "source_sha": environment.head_sha,
            "measured_source_sha": str(web.get("source_sha") or ""),
            "cleanup_command": cleanup_command(environment),
            "retained_at": utcnow(),
        }
        if self.artifacts is not None:
            self.artifacts.mkdir(parents=True, exist_ok=True)
            (self.artifacts / "retained-demo.json").write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8"
            )
        return record

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
    "SCOPE_BY_FAMILY",
    "BLOCKED",
    "POST_MERGE",
    "PREVIEW",
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
    "cleanup_command",
    "grade",
    "provenance_problem",
]
