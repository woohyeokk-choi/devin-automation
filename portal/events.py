"""Structured event log: SQLite on a Docker volume + identical JSON on stdout.

The row written to SQLite, the line printed to stdout and the JSONL export are
produced from the *same* already-sanitized dict, so no channel can be safer or
leakier than another.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc         TEXT NOT NULL,
    trace_id       TEXT NOT NULL,
    request_id     TEXT,
    step_index     INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    scenario       TEXT,
    operation      TEXT NOT NULL,
    actor          TEXT NOT NULL,
    environment_kind TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    http_status    INTEGER,
    tool_name      TEXT,
    duration_ms    INTEGER,
    message        TEXT,
    input_json     TEXT NOT NULL,
    output_json    TEXT NOT NULL,
    assertion_json TEXT,
    revision_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_trace ON events (trace_id, step_index);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts_utc DESC);

CREATE TABLE IF NOT EXISTS explorations (
    key         TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    spec_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    discarded_at TEXT,
    owner       TEXT NOT NULL,
    tab_id      TEXT NOT NULL
);
"""

OUTCOMES = ("ok", "assertion_failed", "expected_denial", "blocked", "error")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EventStore:
    def __init__(self, db_path: Path, stream: Any = None) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stream = stream if stream is not None else sys.stdout
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------- writing
    def emit(self, event: dict[str, Any]) -> dict[str, Any]:
        """Persist and print one already-sanitized event."""
        if event["outcome"] not in OUTCOMES:
            raise ValueError(f"unknown outcome {event['outcome']!r}")
        record = {
            "ts_utc": event.get("ts_utc") or utcnow(),
            "trace_id": event["trace_id"],
            "request_id": event.get("request_id"),
            "step_index": event["step_index"],
            "kind": event["kind"],
            "outcome": event["outcome"],
            "scenario": event.get("scenario"),
            "operation": event["operation"],
            "actor": event["actor"],
            "environment_kind": event["environment_kind"],
            "run_id": event["run_id"],
            "http_status": event.get("http_status"),
            "tool_name": event.get("tool_name"),
            "duration_ms": event.get("duration_ms"),
            "message": event.get("message"),
            "input": event.get("input") or {},
            "output": event.get("output") or {},
            "assertion": event.get("assertion"),
            "revision": event.get("revision") or {},
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO events (ts_utc, trace_id, request_id, step_index, kind,
                    outcome, scenario, operation, actor, environment_kind, run_id,
                    http_status, tool_name, duration_ms, message, input_json,
                    output_json, assertion_json, revision_json)
                VALUES (:ts_utc, :trace_id, :request_id, :step_index, :kind,
                    :outcome, :scenario, :operation, :actor, :environment_kind,
                    :run_id, :http_status, :tool_name, :duration_ms, :message,
                    :input_json, :output_json, :assertion_json, :revision_json)
                """,
                {
                    **{k: record[k] for k in record if k not in ("input", "output", "assertion", "revision")},
                    "input_json": json.dumps(record["input"]),
                    "output_json": json.dumps(record["output"]),
                    "assertion_json": (
                        json.dumps(record["assertion"]) if record["assertion"] else None
                    ),
                    "revision_json": json.dumps(record["revision"]),
                },
            )
            self._conn.commit()
            print(json.dumps(record, sort_keys=True), file=self._stream, flush=True)
        return record

    # ------------------------------------------------------------- reading
    def _row_to_event(self, row: sqlite3.Row) -> dict[str, Any]:
        event = dict(row)
        event["input"] = json.loads(event.pop("input_json"))
        event["output"] = json.loads(event.pop("output_json"))
        assertion = event.pop("assertion_json")
        event["assertion"] = json.loads(assertion) if assertion else None
        event["revision"] = json.loads(event.pop("revision_json"))
        return event

    def recent(self, limit: int = 100, outcome: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if outcome:
            sql += " WHERE outcome = ?"
            params.append(outcome)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_event(r) for r in self._conn.execute(sql, params)]

    def trace(self, trace_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE trace_id = ? ORDER BY step_index, id",
            (trace_id,),
        )
        return [self._row_to_event(r) for r in rows]

    def traces(self, limit: int = 50) -> list[dict[str, Any]]:
        """Summarise traces, keeping the two kinds of failure apart.

        `baseline_defects` are the product contract failures this environment
        exists to reproduce; `harness_failures` are the portal's own checks and
        are the only ones that mean the automation is broken.
        """
        rows = self._conn.execute(
            """
            SELECT trace_id,
                   MIN(ts_utc) AS started_at,
                   COUNT(*)    AS steps,
                   MAX(scenario) AS scenario,
                   MAX(actor)  AS actor,
                   SUM(outcome = 'assertion_failed') AS failures,
                   SUM(outcome = 'assertion_failed'
                       AND assertion_json LIKE '%\"known_baseline_defect\": true%')
                       AS baseline_defects,
                   SUM(outcome = 'expected_denial') AS expected_denials,
                   SUM(outcome IN ('blocked', 'error')) AS blocked
            FROM events GROUP BY trace_id ORDER BY MIN(id) DESC LIMIT ?
            """,
            (limit,),
        )
        summaries = [dict(r) for r in rows]
        for item in summaries:
            item["harness_failures"] = item["failures"] - item["baseline_defects"]
        return summaries

    def export_jsonl(self, trace_id: str | None = None) -> Iterator[str]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if trace_id:
            sql += " WHERE trace_id = ?"
            params.append(trace_id)
        sql += " ORDER BY id"
        for row in self._conn.execute(sql, params):
            yield json.dumps(self._row_to_event(row), sort_keys=True) + "\n"

    # -------------------------------------------------------- explorations
    def save_exploration(
        self, key: str, label: str, spec: dict[str, Any], owner: str, tab_id: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO explorations
                   (key, label, spec_json, created_at, discarded_at, owner, tab_id)
                   VALUES (?, ?, ?, ?, NULL, ?, ?)""",
                (key, label, json.dumps(spec), utcnow(), owner, tab_id),
            )
            self._conn.commit()

    def mark_discarded(self, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE explorations SET discarded_at = ? WHERE key = ?",
                (utcnow(), key),
            )
            self._conn.commit()

    def explorations(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM explorations ORDER BY created_at DESC LIMIT 50"
        )
        out = []
        for row in rows:
            item = dict(row)
            item["spec"] = json.loads(item.pop("spec_json"))
            out.append(item)
        return out

    def exploration(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM explorations WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["spec"] = json.loads(item.pop("spec_json"))
        return item

    def clear_explorations(self) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM explorations")
            self._conn.commit()
            return cursor.rowcount
