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

A request handler only ever *proposes*. Claiming the single-flight slot,
creating the issue and the session, polling and verifying all happen on the
worker (`advance`), so a customer response never waits on api.github.com and a
second incident queued behind the first starts by itself when the slot frees.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

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

-- The single-flight claim. One row can exist, so the database — not a
-- read-then-act check inside one process — decides which repair may create,
-- dispatch and be verified. It is taken before the first remote write and
-- released only when nothing can still be running under it.
CREATE TABLE IF NOT EXISTS repair_slot (
    slot       INTEGER PRIMARY KEY CHECK (slot = 1),
    repair_id  INTEGER NOT NULL,
    claimed_at TEXT NOT NULL
);
"""

#: Repair states. `candidate` means a PR exists and verification has not run;
#: it is never a success.
PROPOSED = "proposed"
DISPATCHED = "dispatched"
CANDIDATE = "candidate"
VERIFIED = "verified_in_preview"
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
        """The repair holding the single-flight claim, if any."""
        row = self._conn.execute(
            "SELECT r.* FROM repair_slot s JOIN repairs r ON r.id = s.repair_id"
        ).fetchone()
        return dict(row) if row else None

    def queued(self) -> list[dict[str, Any]]:
        """Proposals waiting for the slot, oldest first.

        This is the whole queue: a proposal is written by the request handler
        and picked up here by the worker, so nothing is lost if the process
        dies between the two.
        """
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM repairs WHERE state = ? ORDER BY id", (PROPOSED,)
            ).fetchall()
        ]

    # --- single-flight claim -----------------------------------------------

    def claim_slot(self, repair_id: int) -> bool:
        """Take the one slot, or lose to whoever holds it.

        `INSERT OR IGNORE` against a single-row primary key is the whole of
        the mutual exclusion: two connections, two workers, or one process
        restarted all contend on the same row and exactly one wins. The claim
        spans creation, dispatch and verification, so a repair that is still
        `proposed` but already creating an issue blocks the next one.
        """
        with self._lock, self._conn:
            # The insert takes the write lock; a second connection waits on
            # it and then reads the winner it just lost to.
            self._conn.execute(
                "INSERT OR IGNORE INTO repair_slot (slot, repair_id, claimed_at) "
                "VALUES (1, ?, ?)",
                (repair_id, utcnow()),
            )
            row = self._conn.execute(
                "SELECT repair_id FROM repair_slot WHERE slot = 1"
            ).fetchone()
        return row is not None and int(row["repair_id"]) == repair_id

    def slot_claimed_at(self) -> str:
        row = self._conn.execute(
            "SELECT claimed_at FROM repair_slot WHERE slot = 1"
        ).fetchone()
        return str(row["claimed_at"]) if row else ""

    def slot_holder(self) -> int | None:
        row = self._conn.execute(
            "SELECT repair_id FROM repair_slot WHERE slot = 1"
        ).fetchone()
        return int(row["repair_id"]) if row else None

    def release_slot(self, repair_id: int) -> None:
        """Give the slot back. Only for a repair with nothing left running."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM repair_slot WHERE slot = 1 AND repair_id = ?", (repair_id,)
            )

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

    def stale_intents(self, before_utc: str) -> list[dict[str, Any]]:
        """Creation claims nobody settled: their creator is gone."""
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM repair_intents WHERE state = ? AND updated_at < ?",
                (INTENDED, before_utc),
            ).fetchall()
        ]

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
        incident_of: Callable[[int], dict[str, Any] | None] | None = None,
        verifier: Any = None,
        stale_after_minutes: int = 15,
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
        # How the worker re-reads the incident a queued proposal came from:
        # the brief is rebuilt from stored evidence at dispatch time, not
        # carried in memory from the request that observed it.
        self.incident_of = incident_of or (lambda _id: None)
        self.verifier = verifier
        self.stale_after_minutes = stale_after_minutes

    # --- proposal ----------------------------------------------------------

    def consider(self, incident: dict[str, Any]) -> Decision:
        """Called for every incident the store admits, from the request path.

        This writes a row and returns. No slot is claimed and nothing is sent,
        so the customer's response time is the portal's, not GitHub's; the
        worker picks the proposal up on its next pass.
        """
        if incident.get("admission") != "eligible":
            return Decision("skipped", incident.get("admission_reason") or "not eligible")
        if incident.get("state") == VERIFIED:
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
        if repair["state"] in (TERMINAL, NEEDS_ATTENTION, VERIFIED):
            return Decision(
                "skipped",
                repair["terminal_reason"] or repair["attention"] or "",
                int(repair["id"]),
            )
        if repair["state"] in (DISPATCHED, CANDIDATE):
            return Decision("in_flight", "", int(repair["id"]))
        return Decision("queued", "waiting for the worker to claim the slot", int(repair["id"]))

    # --- the worker ---------------------------------------------------------

    def advance(self) -> list[Decision]:
        """One bounded worker pass. The only place network work starts.

        Order matters: settle creation claims whose worker died, then walk the
        repair holding the slot, and only when the slot is free take the next
        queued proposal. One repair is in flight at a time, enforced by the
        database, so this is safe to run from more than one process.
        """
        if not self.dispatch_enabled:
            return []
        decisions: list[Decision] = list(self.recover(self.stale_after_minutes))
        active = self.store.active()
        if active is not None:
            decisions.append(self._walk(active))
            return decisions
        for queued in self.store.queued():
            if not self.store.claim_slot(int(queued["id"])):
                decisions.append(
                    Decision(
                        "deferred",
                        f"repair {self.store.slot_holder()} holds the claim",
                        int(queued["id"]),
                    )
                )
                break
            incident = self.incident_of(int(queued["incident_id"]))
            if incident is None:
                self.store.update(
                    int(queued["id"]),
                    state=NEEDS_ATTENTION,
                    attention="the incident this proposal came from is gone",
                )
                self.store.release_slot(int(queued["id"]))
                decisions.append(Decision("parked", "incident missing", int(queued["id"])))
                continue
            decisions.append(self.dispatch(int(queued["id"]), incident))
            break
        return decisions

    def _walk(self, active: dict[str, Any]) -> Decision:
        repair_id = int(active["id"])
        state = str(active["state"])
        if state in (NEEDS_ATTENTION, TERMINAL, VERIFIED):
            # The slot is held on purpose: something may still be running
            # remotely, and nobody but an operator may decide otherwise.
            return Decision("parked", active["attention"] or active["terminal_reason"] or "", repair_id)
        if state == CANDIDATE:
            return self.verify(repair_id)
        if state == PROPOSED:
            # The slot is claimed but nothing was created yet, which normally
            # means another worker is inside `dispatch` right now. Taking it
            # over immediately is how two sessions get created for one
            # repair, so it is only picked up once the claim has gone stale
            # — and even then dispatch reconciles by marker before creating.
            claimed_at = self.store.slot_claimed_at()
            if claimed_at and not self._stale(claimed_at):
                return Decision(
                    "in_flight", "another worker is creating this repair", repair_id
                )
            incident = self.incident_of(int(active["incident_id"]))
            if incident is None:
                return Decision("parked", "incident missing", repair_id)
            return self.dispatch(repair_id, incident)
        return self.poll(repair_id)

    def _stale(self, timestamp: str) -> bool:
        try:
            claimed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return True
        if claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=timezone.utc)
        return claimed < self._now() - timedelta(minutes=self.stale_after_minutes)

    def _propose(self, incident: dict[str, Any]) -> dict[str, Any]:
        attempt = 1
        body = brief.issue_body(incident, attempt, self.versions)
        request = brief.session_request(
            incident, attempt, self.versions, issue_url="", acu_limit=self.budget.acu_limit
        )
        # The clock starts when paid work does, not when a disabled proposal
        # is written: an empty deadline means "never activated".
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
                "deadline_utc": "",
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
            # The claim stays held: an unknown remote write may have started a
            # job, and releasing it would let a second one begin.
            self.store.update(repair_id, state=NEEDS_ATTENTION, attention=str(exc))
            return Decision("parked", str(exc), repair_id)
        deadline = self._now() + timedelta(minutes=self.budget.wall_clock_minutes)
        self.store.update(
            repair_id,
            state=DISPATCHED,
            deadline_utc=repair["deadline_utc"] or deadline.isoformat(),
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
        try:
            session = self.devin.get_session(str(repair["session_id"]))
        except (RuntimeError, Ambiguous, Refused) as exc:
            # A 401/403/429 or a lost connection leaves the job running: make
            # it visible instead of raising into the caller, and keep the claim.
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                attention=f"session state could not be read: {exc}",
            )
            return Decision("parked", f"poll failed: {exc}", repair_id)
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
        delivered = session.agent_finished or bool(session.structured_output)
        if session.waiting and not delivered:
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                attention=f"session is {session.status_detail}",
            )
            return Decision("waiting", str(session.status_detail), repair_id)

        if delivered:
            # A session that has reported its result and then asked the
            # operator a question has still delivered a candidate. Parking it
            # would leave a pull request unverified over a question nobody in
            # this loop was going to answer; the candidate is verified on its
            # own evidence, and feedback still goes to this same session.
            return self._candidate(repair_id, session)

        exceeded = self._exceeded(repair, session.acus_consumed)
        if exceeded:
            # Only here, and only with nothing to verify: terminating a
            # candidate would destroy the session verification must talk to.
            self._stop(repair_id, exceeded)
            return Decision("stopped", exceeded, repair_id)
        return Decision("running", str(session.status_detail or ""), repair_id)

    def _exceeded(self, repair: dict[str, Any], acus: float) -> str:
        if acus >= float(repair["acu_limit"]):
            return f"ACU budget exhausted: {acus} of {repair['acu_limit']} consumed"
        deadline = str(repair["deadline_utc"] or "")
        if deadline and self._now() > datetime.fromisoformat(deadline):
            return f"wall-clock deadline {deadline} passed"
        return ""

    def _stop(self, repair_id: int, reason: str) -> None:
        repair = self.store.get(repair_id)
        if repair is None:
            return
        stopped = True
        if self.devin is not None and repair["session_id"]:
            try:
                self.devin.terminate_session(str(repair["session_id"]))
            except Refused as exc:
                stopped = False
                reason = f"{reason}; termination refused ({exc})"
            except (RuntimeError, Ambiguous) as exc:
                stopped = False
                reason = f"{reason}; termination outcome unknown ({exc})"
        self.store.update(repair_id, state=TERMINAL, terminal_reason=reason)
        if stopped:
            self.store.release_slot(repair_id)
        else:
            # The session may still be burning ACUs: hold the claim so no
            # second repair starts while this one is unresolved.
            self.store.update(
                repair_id,
                attention="session may still be running; the claim is held "
                "until someone resolves it",
            )

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
            # The agent finished and there is no candidate to verify: nothing
            # can still be running under this claim.
            self.store.release_slot(repair_id)
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
        problem = self._pr_problem(head)
        if problem:
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                agent_pr_url=pr_url,
                attention=f"pull request not usable: {problem}",
            )
            return Decision("parked", problem, repair_id)
        self.store.update(
            repair_id,
            state=CANDIDATE,
            agent_pr_url=pr_url,
            pr_head_sha=head["head_sha"],
        )
        return Decision("candidate", pr_url, repair_id)

    def _pr_problem(self, head: dict[str, str]) -> str:
        """Why this pull request cannot be verified, or an empty string.

        GitHub is the source of truth here, not the agent's own report: the
        branch must live in the target fork, target the agreed base, and name
        one full commit that still exists on an open pull request. Phase 5
        adds the path and diff checks on top of this.
        """
        if head["base_ref"] != brief.BASE_BRANCH:
            return f"targets {head['base_ref'] or 'an unknown branch'}, not {brief.BASE_BRANCH}"
        if head["head_repo"] != self.target_repo:
            return (
                "the head branch lives in "
                f"{head['head_repo'] or 'an unknown repository'}, not {self.target_repo}"
            )
        sha = head["head_sha"].lower()
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            return "the head SHA is not a full commit id"
        if head["merged"] == "true":
            return "the pull request is already merged"
        if head["state"] != "open":
            return f"the pull request is {head['state'] or 'in an unreported state'}"
        return ""

    # --- recovery ----------------------------------------------------------

    def recover(self, stale_after_minutes: int = 15) -> list[Decision]:
        """Settle creation claims whose worker never came back.

        An `intended` row that nobody confirmed means a remote object may or
        may not exist. It is parked for a human, never retried blindly, and
        the single-flight claim stays where it is.
        """
        cutoff = (self._now() - timedelta(minutes=stale_after_minutes)).isoformat()
        decisions: list[Decision] = []
        for intent in self.store.stale_intents(cutoff):
            repair_id = int(intent["repair_id"])
            self.store.settle(
                int(intent["id"]), AMBIGUOUS, "the worker stopped before settling this"
            )
            reason = (
                f"a {intent['kind']} may have been created for marker "
                f"{intent['marker']} but was never confirmed; resolve it by hand"
            )
            self.store.update(repair_id, state=NEEDS_ATTENTION, attention=reason)
            decisions.append(Decision("parked", reason, repair_id))
        return decisions

    # --- feedback ----------------------------------------------------------

    def verify(self, repair_id: int) -> Decision:
        """Grade the candidate independently, then act on the verdict.

        Three outcomes, three different things to do: a pass is the only
        thing that may say `verified_in_preview` and the only thing that frees
        the slot on success; a product failure goes back to the same session
        as expected/observed lines; a blocked run asks for attention, because
        telling a session to change product code over a stack that never came
        up is how a good fix gets reverted.
        """
        repair = self.store.get(repair_id)
        if repair is None:
            return Decision("skipped", "no such repair", repair_id)
        if self.verifier is None:
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                attention="a candidate is waiting but no verifier is configured",
            )
            return Decision("parked", "no verifier configured", repair_id)
        incident = self.incident_of(int(repair["incident_id"])) or {}
        outcome = self.verifier.verify(repair, incident)
        record = {
            "verdict": outcome.verdict,
            "reason": outcome.reason,
            "candidate_sha": outcome.candidate_sha,
            "attempt_id": outcome.record_id,
            "at": self._now().isoformat(),
        }
        self.store.update(repair_id, verification=json.dumps(record))

        if outcome.verdict == "passed":
            self.store.update(
                repair_id,
                state=VERIFIED,
                terminal_reason=f"independently verified on {outcome.candidate_sha[:12]}",
            )
            # Verified and nothing left running: the next queued repair may go.
            self._release_after_verification(repair_id)
            return Decision("verified", outcome.candidate_sha, repair_id)
        if outcome.verdict == "blocked":
            self.store.update(
                repair_id,
                state=NEEDS_ATTENTION,
                attention=f"verification could not answer the question: {outcome.reason}",
            )
            return Decision("blocked", outcome.reason, repair_id)
        return self.feedback(repair_id, list(outcome.failures), outcome.candidate_sha)

    def _release_after_verification(self, repair_id: int) -> None:
        """Stop the session if it is still open, then free the claim.

        A termination whose outcome is unknown keeps the claim: "probably
        stopped" is not a reason to let a second repair start.
        """
        repair = self.store.get(repair_id)
        if repair is None:
            return
        if self.devin is not None and repair["session_id"]:
            try:
                self.devin.terminate_session(str(repair["session_id"]))
            except (RuntimeError, Ambiguous, Refused) as exc:
                self.store.update(
                    repair_id,
                    attention=(
                        "the fix is verified, but the session could not be "
                        f"confirmed stopped ({exc}); the claim is held"
                    ),
                )
                return
        self.store.release_slot(repair_id)

    def feedback(
        self, repair_id: int, failures: list[str], candidate_sha: str = ""
    ) -> Decision:
        """Return a failed verification to the same session, at most twice."""
        repair = self.store.get(repair_id)
        if repair is None or self.devin is None:
            return Decision("skipped", "nothing to talk to", repair_id)
        if repair["state"] != CANDIDATE:
            return Decision("skipped", f"repair is {repair['state']}", repair_id)
        # Preserving a candidate for verification does not buy it more paid
        # work: the budget is checked before anything is sent.
        exceeded = self._exceeded(repair, float(repair["agent_acus"] or 0.0))
        if exceeded:
            self._stop(repair_id, exceeded)
            return Decision("stopped", exceeded, repair_id)
        if int(repair["follow_ups"]) >= MAX_FOLLOW_UPS:
            self._stop(
                repair_id,
                f"{MAX_FOLLOW_UPS} repair follow-ups used without a verified fix",
            )
            return Decision("stopped", "follow-up limit reached", repair_id)
        message = brief.follow_up_message(
            failures, str(repair["agent_pr_url"] or ""), str(repair["pr_head_sha"] or "")
        )
        # Keyed by the commit that failed, so polling the same candidate again
        # finds a confirmed intent and says nothing twice; a new commit is a
        # new marker and may be answered once.
        sha = candidate_sha or str(repair["pr_head_sha"] or "")
        mark = f"{repair['fingerprint']}:{sha or 'no-sha'}:follow-up"
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
