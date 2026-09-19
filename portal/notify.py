"""Outbound Slack status messages, sent only by the trusted coordinator.

An operator watching this lifecycle wants four moments: a repair session
started, a pull request exists, the independent replay answered, and something
needs a human. Everything else — polling, deferrals, an expected denial that
was never an incident in the first place — is noise and is not sent.

Three properties this module is built around:

* **Delivery cannot change a repair.** Notifications are published *after* the
  controller has already written its decision, from the worker rather than
  from inside `Controller`, and a webhook that is missing, malformed or down
  produces a ledger row and nothing else. Nothing here can start a session.
* **Nothing is sent twice on purpose.** Every message has a stable id derived
  from what happened (`repair 2 was verified on fe266eac`), and the ledger's
  unique index is what makes a repeated worker pass, a restart or a second
  backfill a no-op. This is not exactly-once: a request that times out after
  the body was written may have been delivered, and re-sending it would
  duplicate, so an ambiguous attempt is recorded as `unknown` and never
  retried.
* **The webhook is a secret with no other home.** It is read from the
  environment of this process, validated against Slack's host and path,
  redirects are refused, and it is scrubbed out of anything written down. It
  is never placed in a prompt, an issue body, an event, or a candidate
  container.

The CLI is the only way to send anything by hand:

    python3 -m portal.notify test                # one marked connectivity test
    python3 -m portal.notify backfill --repair 1 # one historical summary
    python3 -m portal.notify status              # the ledger, no network
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .config import Settings, settings
from .controller import (
    CANDIDATE,
    DISPATCHED,
    NEEDS_ATTENTION,
    TERMINAL,
    VERIFIED,
)
from .events import utcnow
from .redaction import scrub_text
from .transport import Ambiguous, HttpTransport, Refused, Transport, is_simulated

WEBHOOK_HOST = "hooks.slack.com"
WEBHOOK_PATH = "/services/"

#: Why a simulated worker refuses to deliver, in the ledger where an operator
#: sees it rather than in a comment.
SIMULATED_REFUSAL = "simulated repair: real delivery refused"

PENDING, SENT, FAILED, UNKNOWN, DISABLED = (
    "pending",
    "sent",
    "failed",
    "unknown",
    "disabled",
)

#: Attempts per message, and how long to wait before each retry. A status
#: message is worth a few tries and no more: the console and the ledger hold
#: the authoritative record, so a Slack outage must not become a busy loop.
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (0, 30, 120, 600)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    repair_id   INTEGER,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    state       TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    next_try_at TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def redact_webhook(text: str, webhook: str) -> str:
    """Remove the webhook from anything about to be written down.

    `scrub_text` already erases Slack webhook URLs by shape; this also covers
    a configured value that does not look like one, so a misconfiguration
    cannot publish itself through an error message.
    """
    out = scrub_text(text)
    if webhook and webhook in out:
        out = out.replace(webhook, "<redacted webhook>")
    return out


def webhook_problem(url: str) -> str:
    """Why this value cannot be used as a Slack webhook, or an empty string."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return "the webhook must be an https URL"
    if parsed.hostname != WEBHOOK_HOST:
        return f"the webhook host must be {WEBHOOK_HOST}"
    if not parsed.path.startswith(WEBHOOK_PATH):
        return f"the webhook path must start with {WEBHOOK_PATH}"
    return ""


def escape(text: str) -> str:
    """Slack's three reserved characters, so a payload cannot forge markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class NotificationLog:
    """The durable ledger: one row per message, whatever happened to it."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    def enqueue(
        self, event_id: str, kind: str, text: str, repair_id: int | None = None
    ) -> dict[str, Any] | None:
        """Record a message to send, or return None if it is already known.

        The unique `event_id` is the whole of the deduplication: a worker that
        sees the same transition on its next pass, a restarted coordinator and
        a repeated backfill all land on a row that already exists.
        """
        now = utcnow()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO notifications "
                "(event_id, repair_id, kind, text, state, next_try_at, "
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, repair_id, kind, text, PENDING, now, now, now),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT * FROM notifications WHERE event_id = ?", (event_id,)
            ).fetchone()
        return dict(row)

    def due(self, now: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notifications WHERE state = ? AND next_try_at <= ? "
                "ORDER BY id",
                (PENDING, now),
            ).fetchall()
        return [dict(row) for row in rows]

    def settle(
        self,
        notification_id: int,
        state: str,
        detail: str = "",
        next_try_at: str = "",
        *,
        tried: bool = True,
    ) -> None:
        """Write the outcome of one delivery.

        `tried=False` is for outcomes that never reached the network, so a
        message recorded while no webhook was configured does not read as a
        failed attempt.
        """
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE notifications SET state = ?, detail = ?, "
                f"attempts = attempts + {1 if tried else 0}, "
                "next_try_at = COALESCE(NULLIF(?, ''), next_try_at), updated_at = ? "
                "WHERE id = ?",
                (state, detail, next_try_at, utcnow(), notification_id),
            )

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM notifications WHERE event_id = ?", (event_id,)
            ).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notifications ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def totals(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM notifications GROUP BY state"
            ).fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}


@dataclass
class Notifier:
    """Publishes status lines to one Slack channel, or to nowhere.

    With no webhook configured this is a ledger and nothing else: messages are
    recorded as `disabled` so the lifecycle stays readable, and no request is
    ever attempted.
    """

    log: NotificationLog
    webhook: str = ""
    transport: Transport | None = None
    max_attempts: int = MAX_ATTEMPTS
    now: Callable[[], datetime] | None = None
    #: The repairs this notifier speaks for are simulated. Holding a real
    #: credential is then not permission to use it: the channel is a
    #: production incident feed, and a scripted repair posting into it is
    #: indistinguishable from a real one.
    simulated: bool = False
    problem: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self._clock: Callable[[], datetime] = self.now or (
            lambda: datetime.now(timezone.utc)
        )
        self.problem = webhook_problem(self.webhook) if self.webhook else ""
        if self.simulated and not (
            self.transport is not None and is_simulated(self.transport)
        ):
            # Fail closed, and drop the value rather than remembering it: an
            # ambient SLACK_WEBHOOK_URL must not become reachable through a
            # simulated worker by any later code path.
            self.webhook = ""
            self.problem = SIMULATED_REFUSAL
        # Slack answers a webhook with `ok`, not JSON, and must never be
        # followed to another host: a redirect off hooks.slack.com would post
        # the message body somewhere nobody approved.
        self._wire = self.transport or HttpTransport(timeout=10.0, allow_redirects=False)

    @property
    def enabled(self) -> bool:
        return bool(self.webhook) and not self.problem

    def publish(
        self, event_id: str, kind: str, text: str, repair_id: int | None = None
    ) -> str:
        """Record a message and try to deliver it. Returns its ledger state."""
        if self.simulated:
            text = f"[SIMULATED] {text}"
        row = self.log.enqueue(event_id, kind, text, repair_id)
        if row is None:
            existing = self.log.get(event_id)
            return str(existing["state"]) if existing else SENT
        if not self.enabled:
            self.log.settle(
                int(row["id"]),
                DISABLED,
                self.problem or "no SLACK_WEBHOOK_URL configured",
                tried=False,
            )
            return DISABLED
        return self._attempt(row)

    def deliver_due(self) -> list[str]:
        """Retry everything whose backoff has elapsed. Never raises."""
        if not self.enabled:
            return []
        return [self._attempt(row) for row in self.log.due(self._clock().isoformat())]

    def _attempt(self, row: dict[str, Any]) -> str:
        notification_id = int(row["id"])
        attempts = int(row["attempts"])
        try:
            response = self._wire.request(
                "POST",
                self.webhook,
                headers={"Content-Type": "application/json"},
                json={
                    "text": row["text"],
                    # A status feed must not ping anyone or paste previews of
                    # every pull request it mentions.
                    "unfurl_links": False,
                    "unfurl_media": False,
                    "link_names": False,
                },
            )
        except Ambiguous as exc:
            # The body may have been written. Retrying could post the same
            # line twice, so this stops here and stays visible instead.
            self.log.settle(
                notification_id,
                UNKNOWN,
                redact_webhook(f"delivery outcome unknown ({exc})", self.webhook),
            )
            return UNKNOWN
        except Refused as exc:
            return self._retry(notification_id, attempts, f"refused ({exc})")
        except Exception as exc:  # noqa: BLE001 - a status message may not break a repair
            return self._retry(notification_id, attempts, f"{type(exc).__name__}")
        if response.ok:
            self.log.settle(notification_id, SENT, "")
            return SENT
        return self._retry(notification_id, attempts, response.error())

    def _retry(self, notification_id: int, attempts: int, detail: str) -> str:
        detail = redact_webhook(detail, self.webhook)
        if attempts + 1 >= self.max_attempts:
            self.log.settle(notification_id, FAILED, detail)
            return FAILED
        wait = BACKOFF_SECONDS[min(attempts + 1, len(BACKOFF_SECONDS) - 1)]
        next_try = (self._clock() + timedelta(seconds=wait)).isoformat()
        self.log.settle(notification_id, PENDING, detail, next_try)
        return PENDING


# ------------------------------------------------------------------ messages

PREVIEW_ONLY = "verified in isolated preview; not merged/deployed"

#: Controller decisions worth a message, and what each one is called. Every
#: other action — `running`, `deferred`, `skipped`, `queued`, `in_flight`,
#: `proposed` — is progress noise. An expected denial never appears here at
#: all: it raises no incident, so no repair and no decision exist for it.
NOTIFIED_ACTIONS = frozenset(
    {
        "dispatched",
        "candidate",
        "verified",
        "blocked",
        "followed_up",
        "parked",
        "stopped",
        "waiting",
    }
)


def _case(repair: dict[str, Any], incident: dict[str, Any] | None) -> str:
    scenario = (incident or {}).get("scenario") or ""
    family = (incident or {}).get("family") or ""
    return escape(str(scenario or family or "repair"))


def _links(repair: dict[str, Any]) -> str:
    parts = []
    for label, key in (
        ("issue", "issue_url"),
        ("session", "session_url"),
        ("PR", "agent_pr_url"),
    ):
        value = str(repair.get(key) or "")
        if value:
            parts.append(f"{label} {escape(value)}")
    return " · ".join(parts)


def _mismatch(incident: dict[str, Any] | None) -> str:
    """The first failing assertion as `expected … observed …`.

    What the channel needs is the contract that broke, not the whole trace:
    the issue and the console hold the rest.
    """
    for event in (incident or {}).get("events") or []:
        assertion = event.get("assertion") or {}
        if assertion and not assertion.get("holds"):
            return (
                f"`{escape(str(assertion.get('name', '')))}` expected "
                f"`{_short(json.dumps(assertion.get('expected')), 80)}`, observed "
                f"`{_short(json.dumps(assertion.get('observed')), 80)}`"
            )
    return ""


def _operation(incident: dict[str, Any] | None) -> str:
    for event in (incident or {}).get("events") or []:
        operation = str(event.get("operation") or "")
        if operation:
            return escape(operation)
    return ""


def _trace(incident: dict[str, Any] | None) -> str:
    for event in (incident or {}).get("events") or []:
        trace_id = str(event.get("trace_id") or "")
        if trace_id:
            return escape(trace_id)
    return ""


def _eligibility(incident: dict[str, Any] | None) -> str:
    """Why this failure was admitted, as the incident store recorded it.

    A default is not a judgement: admission means a registered case failed
    its contract, and nothing stronger.
    """
    reason = str((incident or {}).get("admission_reason") or "")
    return escape(reason or "registered contract failure on a supported case")


def _reproduction(repair: dict[str, Any]) -> str:
    """What the repair session itself claimed, labelled as its own claim."""
    raw = str(repair.get("agent_output") or "")
    if not raw:
        return "the session reported no structured result yet"
    try:
        output = json.loads(raw)
    except ValueError:
        return "the session's structured result could not be read"
    if not isinstance(output, dict):
        return "the session's structured result could not be read"
    verdict = "confirmed" if output.get("reproduced") else "not reproduced"
    return (
        f"session reports the failure {verdict} "
        f"({escape(str(output.get('classification') or 'unclassified'))}): "
        f"{_short(str(output.get('summary') or ''), 200)}"
    )


def _short(text: str, limit: int = 220) -> str:
    text = " ".join(str(text or "").split())
    return escape(text[:limit] + "…" if len(text) > limit else text)


def message_for(
    action: str, repair: dict[str, Any], incident: dict[str, Any] | None = None
) -> tuple[str, str, str] | None:
    """`(event_id, kind, text)` for a decision, or None if it is not worth one.

    The id is derived from the thing that happened rather than from when it
    was noticed, so the same transition seen twice is one message.
    """
    if action not in NOTIFIED_ACTIONS:
        return None
    repair_id = int(repair["id"])
    case = _case(repair, incident)
    head = str(repair.get("pr_head_sha") or "")
    state = str(repair.get("state") or "")
    stem = f"repair {repair_id} ({case}), incident {repair.get('incident_id')}"

    if action == "dispatched" and state == DISPATCHED:
        session = str(repair.get("session_id") or "")
        occurrences = int((incident or {}).get("occurrence_count") or 1)
        return (
            f"{repair_id}:session:{session}",
            "investigation_started",
            # Only an eligibility rule has run at this point. Nothing has
            # reproduced anything, so this says suspected and says who
            # decides.
            f"Suspected defect — investigation started. {stem}. "
            f"Failing action `{_operation(incident)}`"
            f"{f': {_mismatch(incident)}' if _mismatch(incident) else ''}. "
            f"Trace {_trace(incident)}, base SHA "
            f"`{escape(str((incident or {}).get('baseline_sha') or '')[:12])}`, "
            f"occurrence {occurrences}. Eligible: {_eligibility(incident)}. "
            f"Plan: the session reproduces first, then fixes in scope and opens a "
            f"pull request, within {repair.get('acu_limit')} requested ACUs and the "
            f"deadline {escape(str(repair.get('deadline_utc') or ''))}; independent "
            f"replay of the pull request SHA decides acceptance. {_links(repair)}",
        )
    if action == "candidate" and state == CANDIDATE:
        return (
            f"{repair_id}:pr:{head}",
            "pull_request",
            f"Pull request available (provisional) — {stem}, head "
            f"`{escape(head[:12])}`. {_reproduction(repair)}. Nothing is accepted "
            f"yet: independent replay of this exact SHA runs next and decides. "
            f"{_links(repair)}",
        )
    if action == "verified":
        return (
            f"{repair_id}:verified:{head}",
            "verified",
            f"Verification passed — {stem}, tested head `{escape(head)}` "
            f"({_checks(repair)}). {_reproduction(repair)}. {PREVIEW_ONLY}. "
            f"{_links(repair)}{_video(repair)}",
        )
    if action == "blocked":
        return (
            f"{repair_id}:blocked:{head}:{repair.get('attempt')}",
            "blocked",
            f"Verification blocked, not completed (no verdict, environment "
            f"problem) — {stem}, head `{escape(head[:12])}`: "
            f"{_short(repair.get('attention'))}. Next: the coordinator retries "
            f"the same SHA; the repair stays unaccepted until a replay produces "
            f"a verdict.",
        )
    if action == "followed_up":
        return (
            f"{repair_id}:failed:{head}",
            "verification_failed",
            f"Verification failed, not completed — {stem}, head "
            f"`{escape(head[:12])}`. Next: follow-up {repair.get('follow_ups')} of 2 "
            f"went back to the same session with the failing checks; no new "
            f"session and no extra budget. {_links(repair)}",
        )
    if action in ("parked", "waiting") and state == NEEDS_ATTENTION:
        return (
            f"{repair_id}:attention:{_fingerprint(repair.get('attention'))}",
            "needs_attention",
            f"Needs a human — {stem}: {_short(repair.get('attention'))}. "
            f"{_links(repair)}",
        )
    if action == "stopped" or state == TERMINAL:
        return (
            f"{repair_id}:terminal:{_fingerprint(repair.get('terminal_reason'))}",
            "terminal",
            f"Repair stopped — {stem}: {_short(repair.get('terminal_reason'))}. "
            f"{_links(repair)}",
        )
    return None


def _checks(repair: dict[str, Any]) -> str:
    """Which replay produced the verdict, so the claim can be looked up.

    The per-check results live in the stored report rather than in a chat
    message: this points at the attempt that holds them.
    """
    raw = str(repair.get("verification") or "")
    if not raw:
        return "replay recorded in the operator console"
    try:
        record = json.loads(raw)
    except ValueError:
        return "replay recorded in the operator console"
    if not isinstance(record, dict):
        return "replay recorded in the operator console"
    return (
        f"registered checks replayed in attempt "
        f"{escape(str(record.get('attempt_id') or '?'))} at "
        f"{escape(str(record.get('at') or ''))}"
    )


def _video(repair: dict[str, Any]) -> str:
    """A recording link only if one exists.

    Nothing in the lifecycle records video, so this is empty unless an
    operator supplies a link (the backfill command does): an invented or
    unrelated recording would be worse than none.
    """
    url = str(repair.get("recording_url") or "")
    return f" · recording {escape(url)}" if url else ""


def _fingerprint(text: Any) -> str:
    return sha256(str(text or "").encode("utf-8")).hexdigest()[:12]


def historical_message(
    repair: dict[str, Any],
    incident: dict[str, Any] | None,
    attempt: dict[str, Any] | None,
    recording_url: str = "",
) -> tuple[str, str, str]:
    """A summary of a result that was reached before Slack existed here.

    Marked as history so nobody reads a backfill as something happening now,
    and keyed by the repair so running the command again sends nothing.
    """
    case = _case(repair, incident)
    head = str(repair.get("pr_head_sha") or "")
    verdict = str((attempt or {}).get("verdict") or "")
    finished = str((attempt or {}).get("finished_at") or repair.get("updated_at") or "")
    checks = str((attempt or {}).get("cases") or "")
    tail = (
        f"{PREVIEW_ONLY}."
        if str(repair.get("state")) == VERIFIED
        else f"state {escape(str(repair.get('state')))}."
    )
    text = (
        f"Historical result — repair ran earlier. {case}, repair {repair['id']}, "
        f"incident {repair.get('incident_id')}: verification {escape(verdict)} at "
        f"{escape(finished)} on head `{escape(head)}` (cases {escape(checks)}). "
        f"{tail} {_links(repair)}"
        f"{_video({**repair, 'recording_url': recording_url})}"
    )
    return f"backfill:{repair['id']}", "historical", text


def test_message(run_id: str) -> tuple[str, str, str]:
    stamp = utcnow()
    return (
        f"connectivity-test:{stamp}",
        "connectivity_test",
        f"Connectivity test from the runtime-repair coordinator ({escape(run_id)}) "
        f"at {stamp}. No repair is running; this message reports nothing about "
        f"any incident.",
    )


# ----------------------------------------------------------------- assembly


def webhook_from_environment() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "").strip()


def build_notifier(
    config: Settings = settings,
    *,
    transport: Transport | None = None,
    simulated: bool = False,
) -> Notifier:
    """The notifier this deployment gets: configured, or a recording no-op.

    `simulated=True` refuses the ambient webhook outright. The channel is a
    production incident feed, so a scripted repair may reach it only through
    a transport the caller passes in deliberately, and says `[SIMULATED]`
    when it does.
    """
    return Notifier(
        log=NotificationLog(config.data_dir / "notifications.sqlite"),
        webhook=webhook_from_environment(),
        transport=transport,
        simulated=simulated,
    )


def _state(config: Settings) -> Any:
    from .coordinator import open_state

    return open_state(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Slack status notifications.")
    parser.add_argument("command", choices=("test", "backfill", "status"))
    parser.add_argument(
        "--repair",
        type=int,
        action="append",
        default=[],
        help="repair id to summarise (repeatable); required for backfill",
    )
    parser.add_argument(
        "--recording",
        default="",
        help=(
            "link to a recording of this repair's replay, included verbatim; "
            "omit it unless the recording shows this repair"
        ),
    )
    args = parser.parse_args(argv)

    notifier = build_notifier(settings)
    if args.command == "status":
        print(
            json.dumps(
                {
                    "webhook_configured": bool(notifier.webhook),
                    "webhook_problem": notifier.problem,
                    "totals": notifier.log.totals(),
                    "messages": [
                        {
                            key: row[key]
                            for key in ("id", "event_id", "kind", "state", "attempts", "detail")
                        }
                        for row in notifier.log.list()
                    ],
                },
                indent=2,
            )
        )
        return 0

    if not notifier.enabled:
        print(
            json.dumps(
                {
                    "sent": False,
                    "reason": notifier.problem or "no SLACK_WEBHOOK_URL configured",
                }
            )
        )
        return 2

    if args.command == "test":
        event_id, kind, text = test_message(settings.run_id)
        print(json.dumps({"event_id": event_id, "state": notifier.publish(event_id, kind, text)}))
        return 0

    if not args.repair:
        raise SystemExit("backfill needs at least one --repair <id>")
    state = _state(settings)
    results = []
    for repair_id in args.repair:
        repair = state.repairs.get(repair_id)
        if repair is None:
            results.append({"repair": repair_id, "state": "unknown repair"})
            continue
        attempts = [
            attempt
            for attempt in state.verifications.for_repair(repair_id)
            if attempt["verdict"] == "passed"
        ]
        incident = state.incidents.get(int(repair["incident_id"]))
        event_id, kind, text = historical_message(
            repair,
            incident,
            attempts[-1] if attempts else None,
            recording_url=args.recording,
        )
        results.append(
            {
                "repair": repair_id,
                "event_id": event_id,
                "state": notifier.publish(event_id, kind, text, repair_id),
            }
        )
    print(json.dumps({"backfilled": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
