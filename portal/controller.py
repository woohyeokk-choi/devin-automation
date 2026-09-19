"""The repair loop: a qualifying incident becomes an issue and a session.

Nobody presses a button. An eligible incident that reaches the store is
considered by the controller; with `AUTO_REPAIR_ENABLED=false` it stops one
step short of the wire and records the exact request bodies it *would* send,
so the disabled path is the same path, minus the call.

The two properties everything else is built around:

* **Creation is intent-first.** The intent row, with its marker, is committed
  before the remote call. A crash, a duplicated event, a second worker or a
  timeout after a successful write all end at the same question — "is there
  already an object carrying this marker?" — which is answered by searching,
  not by creating another one. When that stays unanswerable the repair is
  parked for a human instead of retried.
* **The agent does not grade itself.** Everything the session reports lives in
  `agent_*` columns. `verification` is written only by the independent
  replay, so `status=running, status_detail=finished` can never turn into
  `verified_in_preview`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import brief
from .events import utcnow
from .providers import Devin, GitHub, Session
from .transport import Ambiguous, Refused

SCHEMA = """
CREATE TABLE IF NOT EXISTS repairs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint      TEXT NOT NULL UNIQUE,
    incident_id      INTEGER NOT NULL,
    simulated        INTEGER NOT NULL DEFAULT 0,
    state            TEXT NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 1,
    follow_ups       INTEGER NOT NULL DEFAULT 0,
    terminal_reason  TEXT,
    attention        TEXT,
    issue_title      TEXT NOT NULL,
    issue_body       TEXT NOT NULL,
    session_request  TEXT NOT NULL,
    issue_url        TEXT,
    session_id       TEXT,
    session_url      TEXT,
    acu_limit        INTEGER NOT NULL,
    deadline_utc     TEXT NOT NULL,
    agent_status     TEXT,
    agent_detail     TEXT,
    agent_acus       REAL NOT NULL DEFAULT 0,
    agent_output     TEXT,
    agent_pr_url     TEXT,
    pr_head_sha      TEXT,
    verification     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Durable creation intent. Written and committed *before* the remote call,
-- and the unique key is what makes a repeated delivery a lookup instead of a
-- second issue.
CREATE TABLE IF NOT EXISTS repair_intents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repair_id  INTEGER NOT NULL REFERENCES repairs (id),
    kind       TEXT NOT NULL,
    marker     TEXT NOT NULL,
    state      TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (repair_id, kind, marker)
);
"""

#: Repair states. `candidate` means a PR exists and verification has not run;
#: it is never a success.
PROPOSED = "proposed"
DISPATCHED = "dispatched"
CANDIDATE = "candidate"
NEEDS_ATTENTION = "needs_attention"
TERMINAL = "terminal"

INTENDED, CONFIRMED, AMBIGUOUS = "intended", "confirmed", "ambiguous"

MAX_FOLLOW_UPS = 2
ALLOWED_PR_HOST = "github.com"


class Parked(Exception):
    """Left for an operator. Not retried, not failed silently."""


class Claimed(Exception):
    """Another worker holds the creation claim for this attempt."""


@dataclass(frozen=True)
class Budget:
    """Caps the controller enforces. It may never raise them by itself."""

    acu_limit: int = 20
    wall_clock_minutes: int = 120


@dataclass(frozen=True)
class Decision:
    action: str
    detail: str = ""
    repair_id: int | None = None


def _pr_number(url: str, repo: str) -> int:
    """Accept only a pull request on the fork, and read its number from the URL."""
    prefix = f"https://{ALLOWED_PR_HOST}/{repo}/pull/"
    if not url.startswith(prefix):
        raise ValueError(f"pull request URL is not on {ALLOWED_PR_HOST}/{repo}")
    tail = url[len(prefix):].split("/")[0].split("?")[0]
    if not tail.isdigit():
        raise ValueError("pull request URL has no number")
    return int(tail)


class RepairStore:
    """Repairs and their creation intents. Separate file from the incidents.

    A simulated run points this at its own database, which is why a fake
    provider can never inflate a real incident, a real repair or a verified
    count: there is nothing shared to inflate.
    """

    def __init__(self, db_path: Path, simulated: bool = False) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()
        self.simulated = simulated

    def close(self) -> None:
        self._conn.close()

    def upsert(self, fingerprint: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT id FROM repairs WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            now = utcnow()
            if existing is None:
                columns = ["fingerprint", "created_at", "updated_at", "simulated", *values]
                self._conn.execute(
                    f"INSERT INTO repairs ({','.join(columns)}) "
                    f"VALUES ({','.join('?' * len(columns))})",
                    [fingerprint, now, now, int(self.simulated), *values.values()],
                )
            else:
                self._conn.execute(
                    f"UPDATE repairs SET updated_at = ?, "
                    f"{','.join(f'{k} = ?' for k in values)} WHERE id = ?",
                    [now, *values.values(), int(existing["id"])],
                )
        found = self.by_fingerprint(fingerprint)
        assert found is not None
        return found

    def update(self, repair_id: int, **values: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE repairs SET updated_at = ?, "
                f"{','.join(f'{k} = ?' for k in values)} WHERE id = ?",
                [utcnow(), *values.values(), repair_id],
            )

    def by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM repairs WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return dict(row) if row else None

    def get(self, repair_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM repairs WHERE id = ?", (repair_id,)
        ).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute("SELECT * FROM repairs ORDER BY id DESC").fetchall()
        ]

    def active(self) -> dict[str, Any] | None:
        """The one repair allowed to be in flight."""
        row = self._conn.execute(
            "SELECT * FROM repairs WHERE state IN (?, ?) ORDER BY id LIMIT 1",
            (DISPATCHED, CANDIDATE),
        ).fetchone()
        return dict(row) if row else None

    # --- creation intent ---------------------------------------------------

    def intend(self, repair_id: int, kind: str, marker: str) -> tuple[dict[str, Any], bool]:
        """Record the intent to create a remote object, before creating it.

        The unique key decides who creates: exactly one caller inserts the row
        and gets `claimed=True`. Everyone else — a duplicated delivery, a
        second worker, the same process after a restart — is told to reconcile
        against the remote instead of creating a second object.
        """
        with self._lock, self._conn:
            now = utcnow()
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO repair_intents "
                "(repair_id, kind, marker, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (repair_id, kind, marker, INTENDED, now, now),
            )
            claimed = cursor.rowcount == 1
            row = self._conn.execute(
                "SELECT * FROM repair_intents WHERE repair_id = ? AND kind = ? AND marker = ?",
                (repair_id, kind, marker),
            ).fetchone()
        return dict(row), claimed

    def settle(self, intent_id: int, state: str, detail: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE repair_intents SET state = ?, detail = ?, updated_at = ? WHERE id = ?",
                (state, detail, utcnow(), intent_id),
            )

    def intents(self, repair_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM repair_intents WHERE repair_id = ? ORDER BY id", (repair_id,)
            ).fetchall()
        ]


class Controller:
    """Turns eligible incidents into repair work, or into an exact proposal."""

    def __init__(
        self,
        store: RepairStore,
        *,
        target_repo: str,
        versions: dict[str, Any],
        github: GitHub | None = None,
        devin: Devin | None = None,
        dispatch_enabled: bool = False,
        budget: Budget = Budget(),
        now: Any = None,
    ) -> None:
        if dispatch_enabled and (github is None or devin is None):
            # Missing live configuration is a refusal to dispatch, never a
            # quiet fallback to a fake that would report success.
            raise ValueError("dispatch is enabled but no live providers were supplied")
        self.store = store
        self.target_repo = target_repo
        self.versions = versions
        self.github = github
        self.devin = devin
        self.dispatch_enabled = dispatch_enabled
        self.budget = budget
        self._now = now or (lambda: datetime.now(timezone.utc))

    # --- proposal ----------------------------------------------------------

    def consider(self, incident: dict[str, Any]) -> Decision:
        """Called for every incident the store admits. Idempotent."""
        if incident.get("admission") != "eligible":
            return Decision("skipped", incident.get("admission_reason") or "not eligible")
        if incident.get("state") == "verified_in_preview":
            return Decision("skipped", "already verified")

        existing = self.store.by_fingerprint(incident["fingerprint"])
        repair = existing or self._propose(incident)
        if not self.dispatch_enabled:
            return Decision(
                "proposed",
                "AUTO_REPAIR_ENABLED=false: the issue body and the Devin request "
                "body are recorded, nothing was sent",
                int(repair["id"]),
            )
        if repair["state"] in (TERMINAL, NEEDS_ATTENTION):
            return Decision("skipped", repair["terminal_reason"] or repair["attention"] or "", int(repair["id"]))
        if repair["state"] in (DISPATCHED, CANDIDATE):
            return Decision("in_flight", "", int(repair["id"]))
        active = self.store.active()
        if active is not None and int(active["id"]) != int(repair["id"]):
            return Decision(
                "deferred",
                f"repair {active['id']} is already in flight; one at a time",
                int(repair["id"]),
            )
        return self.dispatch(int(repair["id"]), incident)

    def _propose(self, incident: dict[str, Any]) -> dict[str, Any]:
        attempt = 1
        body = brief.issue_body(incident, attempt, self.versions)
        request = brief.session_request(
            incident, attempt, self.versions, issue_url="", acu_limit=self.budget.acu_limit
        )
        deadline = self._now() + timedelta(minutes=self.budget.wall_clock_minutes)
        return self.store.upsert(
            incident["fingerprint"],
            {
                "incident_id": int(incident["id"]),
                "state": PROPOSED,
                "attempt": attempt,
                "issue_title": brief.issue_title(incident),
                "issue_body": body,
                "session_request": json.dumps(request, indent=2),
                "acu_limit": self.budget.acu_limit,
                "deadline_utc": deadline.isoformat(),
            },
        )

    # --- dispatch ----------------------------------------------------------

    def dispatch(self, repair_id: int, incident: dict[str, Any]) -> Decision:
        repair = self.store.get(repair_id)
        if repair is None:
            return Decision("skipped", "no such repair")
        if self.github is None or self.devin is None:
            return Decision("skipped", "no live providers configured")
        attempt = int(repair["attempt"])
        mark = brief.marker(incident, attempt)
        try:
            issue_url = repair["issue_url"] or self._ensure_issue(repair, mark)
            self.store.update(repair_id, issue_url=issue_url)
            session = self._ensure_session(repair, incident, mark, issue_url)
        except Claimed as exc:
            return Decision("deferred", str(exc), repair_id)
        except Parked as exc:
            self.store.update(repair_id, state=NEEDS_ATTENTION, attention=str(exc))
            return Decision("parked", str(exc), repair_id)
        self.store.update(
            repair_id,
            state=DISPATCHED,
            session_id=session.session_id,
            session_url=session.url,
            agent_status=session.status,
            agent_detail=session.status_detail,
            agent_acus=session.acus_consumed,
        )
        return Decision("dispatched", session.url, repair_id)

    def _ensure_issue(self, repair: dict[str, Any], mark: str) -> str:
        assert self.github is not None
        intent, claimed = self.store.intend(int(repair["id"]), "issue", mark)
        if intent["state"] == CONFIRMED and intent["detail"]:
            return str(intent["detail"])
        # Reuse and post-crash reconciliation are the same lookup.
        found = self.github.find_issue(mark, brief.LABELS)
        if found is not None:
            self.store.settle(int(intent["id"]), CONFIRMED, found.url)
            return found.url
        if not claimed and intent["state"] == INTENDED:
            raise Claimed("another worker is creating the issue for this attempt")
        if intent["state"] == AMBIGUOUS:
            raise Parked(
                "an issue may already have been created for this attempt but "
                "cannot be found; resolve it by hand before dispatching again"
            )
        try:
            issue = self.github.create_issue(
                repair["issue_title"], repair["issue_body"], brief.LABELS
            )
        except Ambiguous as exc:
            self.store.settle(int(intent["id"]), AMBIGUOUS, str(exc))
            raise Parked(f"issue creation outcome unknown ({exc})") from None
        except Refused as exc:
            raise Parked(f"issue creation refused ({exc})") from None
        self.store.settle(int(intent["id"]), CONFIRMED, issue.url)
        return issue.url

    def _ensure_session(
        self, repair: dict[str, Any], incident: dict[str, Any], mark: str, issue_url: str
    ) -> Session:
        assert self.devin is not None
        intent, claimed = self.store.intend(int(repair["id"]), "session", mark)
        if intent["state"] == CONFIRMED and intent["detail"]:
            return self.devin.get_session(str(intent["detail"]))
        found = self.devin.find_tagged(mark)
        if found is not None:
            self.store.settle(int(intent["id"]), CONFIRMED, found.session_id)
            return found
        if not claimed and intent["state"] == INTENDED:
            raise Claimed("another worker is creating the session for this attempt")
        if intent["state"] == AMBIGUOUS:
            raise Parked(
                "a session may already have been created for this attempt but "
                "is not findable by its tag; resolve it by hand"
            )
        request = brief.session_request(
            incident,
            int(repair["attempt"]),
            self.versions,
            issue_url,
            acu_limit=int(repair["acu_limit"]),
        )
        self.store.update(int(repair["id"]), session_request=json.dumps(request, indent=2))
        try:
            session = self.devin.create_session(request)
        except Ambiguous as exc:
            self.store.settle(int(intent["id"]), AMBIGUOUS, str(exc))
            raise Parked(f"session creation outcome unknown ({exc})") from None
        except Refused as exc:
            raise Parked(f"session creation refused ({exc})") from None
        self.store.settle(int(intent["id"]), CONFIRMED, session.session_id)
        return session

    # --- lifecycle ---------------------------------------------------------

    def poll(self, repair_id: int) -> Decision:
        """Read the session's reported state and apply the budget policy."""
        repair = self.store.get(repair_id)
        if repair is None or not repair["session_id"] or self.devin is None:
            return Decision("skipped", "nothing in flight", repair_id)
        session = self.devin.get_session(str(repair["session_id"]))
        self.store.update(
            repair_id,
            agent_status=session.status,
            agent_detail=session.status_detail,
            agent_acus=session.acus_consumed,
            agent_output=json.dumps(session.structured_output) if session.structured_output else None,
        )

        if session.stopped:
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                attention=f"session {session.status} ({session.status_detail})",
            )
            return Decision("parked", f"session {session.status}", repair_id)
        if session.waiting:
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                attention=f"session is {session.status_detail}",
            )
            return Decision("waiting", str(session.status_detail), repair_id)

        if session.agent_finished:
            return self._candidate(repair_id, session)

        exceeded = self._exceeded(repair, session)
        if exceeded:
            # Only here, and only with nothing to verify: terminating a
            # candidate would destroy the session verification must talk to.
            self._stop(repair_id, exceeded)
            return Decision("stopped", exceeded, repair_id)
        return Decision("running", str(session.status_detail or ""), repair_id)

    def _exceeded(self, repair: dict[str, Any], session: Session) -> str:
        if session.acus_consumed >= float(repair["acu_limit"]):
            return (
                f"ACU budget exhausted: {session.acus_consumed} of "
                f"{repair['acu_limit']} consumed"
            )
        if self._now() > datetime.fromisoformat(str(repair["deadline_utc"])):
            return f"wall-clock deadline {repair['deadline_utc']} passed"
        return ""

    def _stop(self, repair_id: int, reason: str) -> None:
        repair = self.store.get(repair_id)
        if repair is None:
            return
        if self.devin is not None and repair["session_id"]:
            try:
                self.devin.terminate_session(str(repair["session_id"]))
            except (RuntimeError, Ambiguous, Refused) as exc:
                reason = f"{reason}; termination failed ({exc})"
        self.store.update(repair_id, state=TERMINAL, terminal_reason=reason)

    def _candidate(self, repair_id: int, session: Session) -> Decision:
        """Agent-finished: record what it claims, verify nothing."""
        output = session.structured_output
        repair = self.store.get(repair_id)
        assert repair is not None
        if not output:
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                attention="session finished without the required structured output",
            )
            return Decision("parked", "no structured output", repair_id)
        if not output.get("reproduced"):
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                attention=(
                    "session did not reproduce the failure before changing code "
                    f"(classification: {output.get('classification')})"
                ),
            )
            return Decision("parked", "not reproduced", repair_id)
        if output.get("classification") != "product_defect":
            self.store.update(
                repair_id,
                state=TERMINAL,
                terminal_reason=f"classified as {output.get('classification')}, no code change",
            )
            return Decision("classified", str(output.get("classification")), repair_id)

        pr_url = str(output.get("pr_url") or "") or (
            session.pull_requests[0] if session.pull_requests else ""
        )
        try:
            number = _pr_number(pr_url, self.target_repo)
            assert self.github is not None
            head = self.github.pull_request_head(number)
        except (ValueError, RuntimeError) as exc:
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                agent_pr_url=pr_url or None,
                attention=f"pull request not usable: {exc}",
            )
            return Decision("parked", str(exc), repair_id)
        if head["base_ref"] != brief.BASE_BRANCH:
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                agent_pr_url=pr_url,
                attention=f"pull request targets {head['base_ref']}, not {brief.BASE_BRANCH}",
            )
            return Decision("parked", "wrong base branch", repair_id)
        self.store.update(
            repair_id,
            state=CANDIDATE,
            agent_pr_url=pr_url,
            pr_head_sha=head["head_sha"],
        )
        return Decision("candidate", pr_url, repair_id)

    # --- feedback ----------------------------------------------------------

    def feedback(self, repair_id: int, failures: list[str]) -> Decision:
        """Return a failed verification to the same session, at most twice."""
        repair = self.store.get(repair_id)
        if repair is None or self.devin is None:
            return Decision("skipped", "nothing to talk to", repair_id)
        if repair["state"] != CANDIDATE:
            return Decision("skipped", f"repair is {repair['state']}", repair_id)
        if int(repair["follow_ups"]) >= MAX_FOLLOW_UPS:
            self._stop(
                repair_id,
                f"{MAX_FOLLOW_UPS} repair follow-ups used without a verified fix",
            )
            return Decision("stopped", "follow-up limit reached", repair_id)
        message = brief.follow_up_message(
            failures, str(repair["agent_pr_url"] or ""), str(repair["pr_head_sha"] or "")
        )
        mark = f"{repair['fingerprint']}:follow-up-{int(repair['follow_ups']) + 1}"
        intent, claimed = self.store.intend(repair_id, "message", mark)
        if intent["state"] == CONFIRMED:
            return Decision("skipped", "this follow-up was already delivered", repair_id)
        if not claimed and intent["state"] == INTENDED:
            return Decision("deferred", "another worker is delivering this follow-up", repair_id)
        if intent["state"] == AMBIGUOUS:
            raise Parked("a follow-up may already have been delivered; resolve by hand")
        try:
            self.devin.send_message(str(repair["session_id"]), message)
        except Ambiguous as exc:
            self.store.settle(int(intent["id"]), AMBIGUOUS, str(exc))
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                attention=f"follow-up delivery outcome unknown ({exc})",
            )
            return Decision("parked", str(exc), repair_id)
        self.store.settle(int(intent["id"]), CONFIRMED, mark)
        self.store.update(
            repair_id, state=DISPATCHED, follow_ups=int(repair["follow_ups"]) + 1
        )
        return Decision("followed_up", mark, repair_id)
