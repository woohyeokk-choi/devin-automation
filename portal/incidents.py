"""Incident model: registered product failures, deduplicated.

Three counts are deliberately kept apart, because collapsing them is how a
console starts lying:

* `event_count`      — sanitized log lines held as evidence.
* `occurrence_count` — distinct failed user actions (traces). One user action
  that trips two sibling assertions is one occurrence, not two.
* incidents          — distinct failure families. The whole point of a
  fingerprint is that the fifth reproduction of S2 is still one incident.

Observation is independent of dispatch: `AUTO_REPAIR_ENABLED=false` stops the
controller from calling anything external, it does not stop the portal from
noticing that the product broke.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import utcnow
from .redaction import scrub

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint       TEXT NOT NULL UNIQUE,
    family            TEXT NOT NULL,
    scenario          TEXT NOT NULL,
    title             TEXT NOT NULL,
    actor             TEXT NOT NULL,
    target_repo       TEXT NOT NULL,
    baseline_sha      TEXT NOT NULL,
    revision_strength TEXT NOT NULL,
    fixture_revision  TEXT,
    state             TEXT NOT NULL DEFAULT 'detected',
    occurrence_count  INTEGER NOT NULL DEFAULT 0,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    issue_url         TEXT,
    session_url       TEXT,
    pr_url            TEXT,
    verification      TEXT
);

CREATE TABLE IF NOT EXISTS incident_events (
    event_id    TEXT PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents (id),
    trace_id    TEXT NOT NULL,
    step_index  INTEGER NOT NULL,
    ts_utc      TEXT NOT NULL,
    operation   TEXT NOT NULL,
    role        TEXT NOT NULL,
    event_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incident_events_incident
    ON incident_events (incident_id, ts_utc);

CREATE TABLE IF NOT EXISTS incident_occurrences (
    incident_id INTEGER NOT NULL REFERENCES incidents (id),
    trace_id    TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    PRIMARY KEY (incident_id, trace_id)
);

CREATE TABLE IF NOT EXISTS incident_processing_errors (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc   TEXT NOT NULL,
    event_id TEXT,
    reason   TEXT NOT NULL
);
"""

#: States an incident can hold. Nothing promotes itself: a user whose next
#: attempt happens to work has not had a bug fixed, and a PR is a candidate
#: until an independent replay says otherwise.
STATES = ("detected", "candidate_fix", "verified_in_preview")

#: Environments whose events are evidence *about* an incident rather than new
#: product failures. They may never create one, which is what stops a repair
#: session's own reproduction from opening a second repair job.
DERIVED_KINDS = frozenset({"preview", "reproduction", "verification"})


@dataclass(frozen=True)
class Family:
    """One product defect, however many assertions or routes observe it."""

    key: str
    scenario: str
    title: str
    statement: str
    assertions: tuple[str, ...]


FAMILIES: tuple[Family, ...] = (
    Family(
        key="discarded_form_data_key_is_reused",
        scenario="S2",
        title="A discarded exploration link comes back pointing at new state",
        statement=(
            "Saving a new exploration after discarding one reuses the discarded "
            "key, so an old link resolves again and shows a different "
            "exploration's state."
        ),
        # Both sides of the same defect: the save reuses the key, and the stale
        # link therefore resolves. One incident, two pieces of evidence.
        assertions=(
            "new_exploration_does_not_reuse_a_discarded_key",
            "discarded_exploration_link_stays_gone",
            "exploration_link_shows_its_own_state",
        ),
    ),
    Family(
        key="omitted_row_limit_is_reset",
        scenario="S1",
        title="An MCP chart update resets fields the caller omitted",
        statement=(
            "Updating only the sort of a table chart through MCP resets the "
            "omitted row limit to the default instead of leaving it alone."
        ),
        assertions=(
            "row_limit_survives_an_unrelated_change",
            "color_scheme_survives_an_unrelated_change",
        ),
    ),
)

_BY_ASSERTION: dict[str, Family] = {
    name: family for family in FAMILIES for name in family.assertions
}


def family_for(assertion_name: str) -> Family | None:
    return _BY_ASSERTION.get(assertion_name)


def baseline_of(revision: dict[str, Any]) -> tuple[str, str]:
    """The SHA an incident is pinned to, and how well it is known.

    An unmeasured or mismatched checkout is not silently treated as the
    baseline: it fingerprints as `unverified`, so incidents raised against an
    unprovable environment never merge with verified ones.
    """
    strength = str(revision.get("strength") or "unmeasured")
    sha = revision.get("checkout_sha")
    if not sha or revision.get("checkout_matches_running_code") is not True:
        return "unverified", strength
    return str(sha), strength


def fingerprint(target_repo: str, baseline_sha: str, family: str, actor: str) -> str:
    """Stable across runs: only what identifies the defect goes in.

    No timestamps, trace/request/event ids, exploration keys or chart ids —
    those differ on every reproduction of the same bug.
    """
    material = "\n".join([target_repo, baseline_sha, family, actor])
    return hashlib.sha256(material.encode()).hexdigest()[:32]


class IncidentStore:
    def __init__(self, db_path: Path, target_repo: str, parent_fingerprint: str = "") -> None:
        self.db_path = db_path
        self.target_repo = target_repo
        # Server-side configuration: which incident a preview/verification run
        # belongs to. A browser field can never supply this.
        self.parent_fingerprint = parent_fingerprint
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    # ---------------------------------------------------------- ingestion
    def observe(self, event: dict[str, Any]) -> dict[str, Any]:
        """Fold one sanitized event into the incident model.

        Returns what was decided, so the caller (and the tests) can tell a
        suppressed control apart from a duplicate delivery apart from a new
        occurrence. Never raises into the request path: a processing failure
        is recorded and surfaced as console freshness, because losing the
        user's action to an incident-engine bug would be worse than the bug.
        """
        try:
            return self._observe(event)
        except Exception as exc:  # pragma: no cover - defensive
            self._record_error(event.get("event_id"), type(exc).__name__)
            return {"action": "processing_error"}

    def _observe(self, event: dict[str, Any]) -> dict[str, Any]:
        assertion = event.get("assertion") or {}
        name = assertion.get("name", "")
        if event.get("outcome") != "assertion_failed":
            # `ok`, `expected_denial` (N1), `blocked` and `error` are never
            # product defects; a denied Gamma user is the control working.
            return {"action": "suppressed", "reason": f"outcome:{event.get('outcome')}"}
        family = family_for(name)
        if family is None:
            return {"action": "suppressed", "reason": "unregistered_failure"}
        if assertion.get("subject") == "harness":
            return {"action": "suppressed", "reason": "harness_failure"}

        event_id = event.get("event_id")
        if not event_id:
            self._record_error(None, "event without an event_id")
            return {"action": "processing_error", "reason": "missing_event_id"}

        derived = str(event.get("environment_kind")) in DERIVED_KINDS
        baseline_sha, strength = baseline_of(event.get("revision") or {})
        actor = str(event.get("actor"))
        print_key = (
            self.parent_fingerprint
            if derived
            else fingerprint(self.target_repo, baseline_sha, family.key, actor)
        )
        if derived and not print_key:
            self._record_error(event_id, "preview event with no configured parent incident")
            return {"action": "suppressed", "reason": "derived_without_parent"}

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT id FROM incidents WHERE fingerprint = ?", (print_key,)
                ).fetchone()
                if row is None:
                    if derived:
                        self._conn.execute("ROLLBACK")
                        self._record_error(event_id, "parent incident does not exist")
                        return {"action": "suppressed", "reason": "unknown_parent"}
                    cursor = self._conn.execute(
                        """INSERT INTO incidents (fingerprint, family, scenario, title,
                               actor, target_repo, baseline_sha, revision_strength,
                               fixture_revision, first_seen_at, last_seen_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            print_key,
                            family.key,
                            family.scenario,
                            family.title,
                            actor,
                            self.target_repo,
                            baseline_sha,
                            strength,
                            (event.get("revision") or {}).get("fixture_revision"),
                            event["ts_utc"],
                            event["ts_utc"],
                        ),
                    )
                    incident_id = int(cursor.lastrowid or 0)
                    created = True
                else:
                    incident_id = int(row["id"])
                    created = False

                # Delivery dedup: the same event replayed is stored once and
                # counts once, whichever thread gets there first.
                inserted = self._conn.execute(
                    """INSERT OR IGNORE INTO incident_events (event_id, incident_id,
                           trace_id, step_index, ts_utc, operation, role, event_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id,
                        incident_id,
                        event["trace_id"],
                        event.get("step_index") or 0,
                        event["ts_utc"],
                        event.get("operation") or "",
                        "derived_evidence" if derived else "failure",
                        # Scrubbed again on the way in: the bundle leaves the
                        # portal, so it gets the sanitizer a second time rather
                        # than trusting that the log already did it.
                        json.dumps(scrub(event), sort_keys=True),
                    ),
                ).rowcount
                if not inserted:
                    self._conn.execute("ROLLBACK")
                    return {
                        "action": "duplicate_event",
                        "incident_id": incident_id,
                        "fingerprint": print_key,
                    }

                new_occurrence = False
                if not derived:
                    # Occurrence dedup: sibling assertions inside one user
                    # action share a trace, so they count once.
                    new_occurrence = bool(
                        self._conn.execute(
                            """INSERT OR IGNORE INTO incident_occurrences
                                   (incident_id, trace_id, ts_utc) VALUES (?, ?, ?)""",
                            (incident_id, event["trace_id"], event["ts_utc"]),
                        ).rowcount
                    )
                    self._conn.execute(
                        """UPDATE incidents
                              SET occurrence_count = occurrence_count + ?,
                                  last_seen_at = MAX(last_seen_at, ?)
                            WHERE id = ?""",
                        (1 if new_occurrence else 0, event["ts_utc"], incident_id),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return {
            "action": "created" if created else "updated",
            "incident_id": incident_id,
            "fingerprint": print_key,
            "family": family.key,
            "new_occurrence": new_occurrence,
            "derived": derived,
        }

    def _record_error(self, event_id: str | None, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO incident_processing_errors (ts_utc, event_id, reason)"
                " VALUES (?, ?, ?)",
                (utcnow(), event_id, reason),
            )

    # ------------------------------------------------------------ reading
    def _counts(self, incident_id: int) -> dict[str, int]:
        row = self._conn.execute(
            """SELECT COUNT(*) AS events,
                      SUM(role = 'derived_evidence') AS derived
                 FROM incident_events WHERE incident_id = ?""",
            (incident_id,),
        ).fetchone()
        return {"event_count": row["events"] or 0, "derived_event_count": row["derived"] or 0}

    def list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM incidents ORDER BY last_seen_at DESC, id DESC"
        ).fetchall()
        return [{**dict(row), **self._counts(int(row["id"]))} for row in rows]

    def get(self, incident_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            return None
        incident = {**dict(row), **self._counts(incident_id)}
        incident["events"] = self.events(incident_id)
        incident["traces"] = self.traces(incident_id)
        incident["processing_errors"] = self.processing_errors()
        return incident

    def by_fingerprint(self, value: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id FROM incidents WHERE fingerprint = ?", (value,)
        ).fetchone()
        return self.get(int(row["id"])) if row else None

    def events(self, incident_id: int) -> list[dict[str, Any]]:
        """Evidence ordered the way it happened: by trace, then request step."""
        rows = self._conn.execute(
            """SELECT event_json FROM incident_events
                WHERE incident_id = ? ORDER BY ts_utc, trace_id, step_index""",
            (incident_id,),
        )
        return [json.loads(row["event_json"]) for row in rows]

    def traces(self, incident_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT trace_id, MIN(ts_utc) AS started_at, COUNT(*) AS evidence,
                      MAX(role) AS role
                 FROM incident_events WHERE incident_id = ?
                GROUP BY trace_id ORDER BY MIN(ts_utc)""",
            (incident_id,),
        )
        return [dict(row) for row in rows]

    def processing_errors(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM incident_processing_errors ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def totals(self) -> dict[str, int]:
        row = self._conn.execute(
            """SELECT COUNT(*) AS incidents,
                      COALESCE(SUM(occurrence_count), 0) AS occurrences
                 FROM incidents"""
        ).fetchone()
        events = self._conn.execute(
            "SELECT COUNT(*) AS n FROM incident_events"
        ).fetchone()["n"]
        return {
            "incidents": row["incidents"],
            "failed_actions": row["occurrences"],
            "events": events,
            "processing_errors": len(self.processing_errors()),
        }
