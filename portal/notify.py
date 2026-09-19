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
* **The credential is a secret with no other home.** The bot token and the
  webhook are read from the environment of this process only, the webhook is
  validated against Slack's host and path, redirects are refused, and both
  are scrubbed out of anything written down. Neither is ever placed in a
  prompt, an issue body, an event, or a candidate container.

There are two wires and only one of them is ever live. A configured
`SLACK_BOT_TOKEN` selects the Web API client, which posts each repair's
updates in that repair's thread and can attach a local clip;
`SLACK_WEBHOOK_URL` is the fallback used when no token is configured. Two
live transports would mean two copies of every message, so the webhook is
dropped whenever the bot exists.

The CLI is the only way to send anything by hand:

    python3 -m portal.notify test                # one marked connectivity test
    python3 -m portal.notify backfill --repair 1 # one historical summary
    python3 -m portal.notify result --repair 1 … # one verified result
    python3 -m portal.notify attach --repair 1 … # one clip, into its thread
    python3 -m portal.notify correction --text … # one correction, keyed by text
    python3 -m portal.notify status              # the ledger, no network
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Sequence
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
from .slack_bot import (
    APPROVED_CHANNEL,
    SOURCE_LABEL,
    SlackBot,
    SlackRejected,
    WebClientBot,
    bot_is_simulated,
    channel_problem,
)
from .transport import Ambiguous, HttpTransport, Refused, Transport, is_simulated

log = logging.getLogger("portal.notify")

WEBHOOK_HOST = "hooks.slack.com"
WEBHOOK_PATH = "/services/"

#: Why a simulated worker refuses to deliver, in the ledger where an operator
#: sees it rather than in a comment.
SIMULATED_REFUSAL = "simulated repair: real delivery refused"
SIMULATED_RECORD_REFUSAL = "simulated repair record: real delivery refused"

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
CREATE TABLE IF NOT EXISTS notification_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_threads (
    repair_id  INTEGER PRIMARY KEY,
    channel    TEXT NOT NULL,
    parent_ts  TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_uploads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id  TEXT NOT NULL UNIQUE,
    repair_id  INTEGER,
    path       TEXT NOT NULL,
    sha        TEXT NOT NULL,
    file_id    TEXT NOT NULL DEFAULT '',
    state      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class HistoricalThread:
    """A result message an operator verified in the channel, and what it said.

    Identified by the repair's own evidence rather than by a row number: a
    repair id is local to one database, so a fresh deployment's repair 1 is
    not this deployment's repair 1 and must not inherit its conversation.
    """

    case: str
    sha: str
    parent_ts: str


#: The result messages the workspace owner verified in the approved channel.
#: Later updates reply under these rather than restating history as new
#: top-level posts. The mapping is supplied, never scraped from channel
#: history, and is matched to a stored repair by case and accepted head.
HISTORICAL_THREADS: tuple[HistoricalThread, ...] = (
    HistoricalThread(
        "S2", "d234055eaf85a70d5e5ec5a7a7256ee43d02dde6", "1789854458.454909"
    ),
    HistoricalThread(
        "S1", "fe266eac51a996760a75997ff94b3270c9ef73b1", "1789854458.664169"
    ),
)

#: When this ledger started speaking for the deployment. Records older than
#: it were reached before Slack existed here and are the backfill command's
#: business, not a restart's.
WATERMARK = "reconcile_since"


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


def clip_upload_id(repair_id: int, recording: Recording, path: Path) -> str:
    """A stable id for one clip of one repair, from its bytes and its commit.

    Derived rather than generated, so a repeated run, a restart or a second
    operator lands on the reservation that already exists instead of
    uploading the same file again.
    """
    digest = sha256(path.read_bytes()).hexdigest()[:12]
    return f"clip:{repair_id}:{recording.sha.strip().lower()[:12]}:{digest}"


def _labelled(text: str) -> str:
    """Name the sender, so this app is never read as the official integration."""
    if text.rstrip().endswith(f"_{SOURCE_LABEL}_"):
        return text
    return f"{text}\n_{SOURCE_LABEL}_"


class NotificationLog:
    """The durable ledger: one row per message, whatever happened to it."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(notifications)")
        }
        if "ts" not in columns:
            self._conn.execute(
                "ALTER TABLE notifications ADD COLUMN ts TEXT NOT NULL DEFAULT ''"
            )
        self._conn.commit()
        self._lock = threading.RLock()

    # --- threads -----------------------------------------------------------

    def bootstrap_threads(self, channel: str, parents: dict[int, str]) -> None:
        """Record known parent messages, never overwriting one already held."""
        now = utcnow()
        with self._lock, self._conn:
            for repair_id, parent_ts in parents.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO notification_threads "
                    "(repair_id, channel, parent_ts, source, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (repair_id, channel, parent_ts, "bootstrap", now),
                )

    def thread_of(self, repair_id: int, channel: str) -> str:
        """The parent `ts` this repair's updates belong under, if one is known."""
        with self._lock:
            row = self._conn.execute(
                "SELECT parent_ts FROM notification_threads "
                "WHERE repair_id = ? AND channel = ?",
                (repair_id, channel),
            ).fetchone()
        return str(row["parent_ts"]) if row else ""

    def remember_thread(self, repair_id: int, channel: str, parent_ts: str) -> None:
        """Adopt a posted message as a repair's parent if it has none yet."""
        if not parent_ts:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO notification_threads "
                "(repair_id, channel, parent_ts, source, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (repair_id, channel, parent_ts, "posted", utcnow()),
            )

    def threads(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notification_threads ORDER BY repair_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_ts(self, notification_id: int, ts: str) -> None:
        if not ts:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE notifications SET ts = ? WHERE id = ?", (ts, notification_id)
            )

    # --- uploads -----------------------------------------------------------

    def claim_upload(
        self, upload_id: str, repair_id: int | None, path: str, sha: str
    ) -> dict[str, Any] | None:
        """Reserve one upload, or return None if this file is already known.

        The reservation is what keeps a repeated worker pass or a restart from
        sending the same clip twice: a row exists before the first byte is
        offered to Slack, and its state is the only record consulted later.
        """
        now = utcnow()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO notification_uploads "
                "(upload_id, repair_id, path, sha, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (upload_id, repair_id, path, sha, PENDING, now, now),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT * FROM notification_uploads WHERE upload_id = ?", (upload_id,)
            ).fetchone()
        return dict(row)

    def settle_upload(
        self, upload_id: str, state: str, detail: str = "", file_id: str = ""
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE notification_uploads SET state = ?, detail = ?, "
                "file_id = COALESCE(NULLIF(?, ''), file_id), updated_at = ? "
                "WHERE upload_id = ?",
                (state, detail, file_id, utcnow(), upload_id),
            )

    def upload(self, upload_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM notification_uploads WHERE upload_id = ?", (upload_id,)
            ).fetchone()
        return dict(row) if row else None

    def uploads(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notification_uploads ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

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

    def watermark(self, now: str) -> str:
        """The instant this ledger took responsibility, written down once.

        Set on first read and never moved, so a restart can tell a repair
        that finished while the coordinator was down from one that finished
        before any of this existed.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO notification_meta (key, value) VALUES (?, ?)",
                (WATERMARK, now),
            )
            row = self._conn.execute(
                "SELECT value FROM notification_meta WHERE key = ?", (WATERMARK,)
            ).fetchone()
        return str(row["value"])

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
    #: The Web API client, when the app is configured with a bot token. It is
    #: preferred over the webhook and never used alongside it: two live
    #: transports would post every line twice.
    bot: SlackBot | None = None
    channel: str = APPROVED_CHANNEL
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
        if self.bot is not None:
            # A bot token can address any channel its scopes reach, so the
            # destination is checked here as well as in the client.
            self.problem = channel_problem(self.channel) or channel_problem(
                self.bot.channel
            )
        if self.simulated and not (
            self.transport is not None and is_simulated(self.transport)
        ):
            # Fail closed, and drop the value rather than remembering it: an
            # ambient SLACK_WEBHOOK_URL must not become reachable through a
            # simulated worker by any later code path.
            self.webhook = ""
            self.problem = SIMULATED_REFUSAL
        if self.simulated and self.bot is not None and not bot_is_simulated(self.bot):
            # The same rule for the token: a scripted repair must not reach a
            # channel people read, whatever credential this process holds.
            self.bot = None
            self.problem = SIMULATED_REFUSAL
        # Slack answers a webhook with `ok`, not JSON, and must never be
        # followed to another host: a redirect off hooks.slack.com would post
        # the message body somewhere nobody approved.
        self._wire = self.transport or HttpTransport(timeout=10.0, allow_redirects=False)

    @property
    def enabled(self) -> bool:
        return (self.bot is not None or bool(self.webhook)) and not self.problem

    @property
    def transport_name(self) -> str:
        """Which of the two wires is live. Never both."""
        if self.bot is not None:
            return "bot"
        return "webhook" if self.webhook else "none"

    def publish(
        self,
        event_id: str,
        kind: str,
        text: str,
        repair_id: int | None = None,
        *,
        simulated_record: bool = False,
    ) -> str:
        """Record a message and try to deliver it. Returns its ledger state.

        `simulated_record` is the stored row's own verdict on itself. A live
        notifier reading a scripted repair out of the database is the same
        danger as a scripted worker holding a real webhook, so it is recorded
        and refused rather than labelled and sent.
        """
        if self.simulated or simulated_record:
            text = f"[SIMULATED] {text}"
        row = self.log.enqueue(event_id, kind, text, repair_id)
        if row is None:
            existing = self.log.get(event_id)
            return str(existing["state"]) if existing else SENT
        if simulated_record and not self.simulated:
            self.log.settle(
                int(row["id"]), DISABLED, SIMULATED_RECORD_REFUSAL, tried=False
            )
            return DISABLED
        if not self.enabled:
            self.log.settle(
                int(row["id"]),
                DISABLED,
                self.problem or "no SLACK_BOT_TOKEN or SLACK_WEBHOOK_URL configured",
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
        if self.bot is not None:
            return self._attempt_bot(self.bot, row)
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

    def _attempt_bot(self, bot: SlackBot, row: dict[str, Any]) -> str:
        """Deliver one row through the Web API, in its repair's thread.

        A repair's first delivered message becomes the parent of the rest, so
        the lifecycle of one incident reads as a conversation and never mixes
        with another repair's.
        """
        notification_id = int(row["id"])
        attempts = int(row["attempts"])
        repair_id = row["repair_id"]
        thread_ts = (
            self.log.thread_of(int(repair_id), self.channel)
            if repair_id is not None
            else ""
        )
        try:
            ts = bot.post(_labelled(str(row["text"])), thread_ts=thread_ts)
        except Ambiguous as exc:
            self.log.settle(
                notification_id,
                UNKNOWN,
                redact_webhook(f"delivery outcome unknown ({exc})", self.webhook),
            )
            return UNKNOWN
        except (Refused, SlackRejected) as exc:
            return self._retry(notification_id, attempts, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a status message may not break a repair
            return self._retry(notification_id, attempts, f"{type(exc).__name__}")
        self.log.record_ts(notification_id, ts)
        if repair_id is not None and not thread_ts:
            self.log.remember_thread(int(repair_id), self.channel, ts)
        self.log.settle(notification_id, SENT, "")
        return SENT

    def attach(
        self,
        upload_id: str,
        path: Path,
        recording: Recording,
        repair: dict[str, Any],
        attempt: dict[str, Any] | None = None,
        *,
        simulated_record: bool = False,
    ) -> dict[str, str]:
        """Upload one existing local clip into its repair's thread.

        The clip must say which capture it is and which commit it ran at, and
        that commit must be the accepted head, or nothing is offered to Slack:
        an unbound file next to a verified result reads as proof of it.

        Matching the head is not enough on its own. A candidate that was never
        accepted has a head too, so the repair must also satisfy the same
        check a result message does — verified, non-simulated, with a stored
        passing attempt measured on exactly that commit — or a clip of an
        unverified build would arrive looking like accepted proof. `attempt`
        defaults to nothing, which refuses.

        The reservation row is written before the upload is attempted and is
        never re-attempted from it, so a repeated run cannot post the same
        clip twice, and a failure part-way through Slack's multi-step upload
        is recorded as `unknown` rather than as a delivered file.
        """
        repair_id = repair.get("id")
        problem = result_problem(repair, attempt) or recording_problem(
            recording, repair
        )
        if problem:
            return {"state": DISABLED, "detail": problem, "file_id": ""}
        if not path.is_file():
            return {"state": DISABLED, "detail": "no such clip file", "file_id": ""}
        if self.simulated or simulated_record:
            return {
                "state": DISABLED,
                "detail": SIMULATED_RECORD_REFUSAL,
                "file_id": "",
            }
        if self.bot is None:
            return {
                "state": DISABLED,
                "detail": "file upload needs SLACK_BOT_TOKEN; the webhook cannot",
                "file_id": "",
            }
        if not self.enabled:
            return {"state": DISABLED, "detail": self.problem, "file_id": ""}
        sha = recording.sha.strip().lower()
        row = self.log.claim_upload(
            upload_id,
            int(repair_id) if repair_id is not None else None,
            str(path),
            sha,
        )
        if row is None:
            known = self.log.upload(upload_id) or {}
            return {
                "state": str(known.get("state") or UNKNOWN),
                "detail": str(known.get("detail") or "already recorded"),
                "file_id": str(known.get("file_id") or ""),
            }
        thread_ts = (
            self.log.thread_of(int(repair_id), self.channel)
            if repair_id is not None
            else ""
        )
        comment = _labelled(
            f"{escape(recording.case)} replay captured {escape(recording.recorded_at)} "
            f"against `{sha[:12]}` — {_short(recording.scope or 'scope unstated', 120)}"
        )
        try:
            file_id = self.bot.upload(
                path,
                title=f"{recording.case} · {sha[:12]}",
                comment=comment,
                thread_ts=thread_ts,
            )
        except Ambiguous as exc:
            # Part of the upload may have been applied. A second attempt could
            # publish the clip twice, so this stays unknown and visible.
            detail = redact_webhook(f"upload outcome unknown ({exc})", self.webhook)
            self.log.settle_upload(upload_id, UNKNOWN, detail)
            return {"state": UNKNOWN, "detail": detail, "file_id": ""}
        except Exception as exc:  # noqa: BLE001 - an upload may not break a repair
            detail = redact_webhook(f"{type(exc).__name__}: {exc}", self.webhook)
            self.log.settle_upload(upload_id, FAILED, detail)
            return {"state": FAILED, "detail": detail, "file_id": ""}
        self.log.settle_upload(upload_id, SENT, "", file_id)
        return {"state": SENT, "detail": "", "file_id": file_id}

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


def _next_after_block(repair: dict[str, Any]) -> str:
    """What follows a blocked attempt, read off the repair rather than hoped.

    A block is an absent verdict, and the controller may answer it with
    another replay or by parking the repair for a person. Promising a retry
    the state does not support tells the channel a repair is still moving
    when nobody is moving it.
    """
    state = str(repair.get("state") or "")
    if state == NEEDS_ATTENTION:
        return f"parked for a human — {_short(repair.get('attention'))}"
    if state == TERMINAL:
        return f"the repair is stopped — {_short(repair.get('terminal_reason'))}"
    if state in (DISPATCHED, CANDIDATE):
        return "the coordinator replays the same SHA"
    return f"decided by the controller from state {escape(state)}"


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
            f"{_links(repair)}",
        )
    if action == "blocked":
        return (
            f"{repair_id}:blocked:{head}:{repair.get('attempt')}",
            "blocked",
            f"Verification blocked, not completed (no verdict, environment "
            f"problem) — {stem}, head `{escape(head[:12])}`: "
            f"{_short(repair.get('attention'))}. Next: {_next_after_block(repair)}; "
            f"the repair stays unaccepted until a replay produces a verdict.",
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


#: The decision a repair's persisted state implies, for records whose
#: announcement may have been lost. A state is the committed fact; the
#: `Decision` that produced it lived only in the process that crashed.
STATE_ACTIONS = {
    DISPATCHED: "dispatched",
    CANDIDATE: "candidate",
    VERIFIED: "verified",
    NEEDS_ATTENTION: "parked",
    TERMINAL: "stopped",
}


def message_for_state(
    repair: dict[str, Any], incident: dict[str, Any] | None = None
) -> tuple[str, str, str] | None:
    """The message this repair's current state deserves, if any."""
    action = STATE_ACTIONS.get(str(repair.get("state") or ""))
    return message_for(action, repair, incident) if action else None


def reconcile(
    repairs: list[dict[str, Any]],
    notifier: Notifier,
    incident_of: Callable[[int], dict[str, Any] | None],
    *,
    since: str = "",
) -> list[str]:
    """Announce committed outcomes whose announcement was lost.

    `advance()` commits a state and the announcement follows it, so a crash
    in between loses that message for good: nothing ever revisits a decision
    object. A restart therefore re-derives each repair's message from the
    row itself and enqueues only the event ids the ledger has never seen.

    Only the current state of each repair is announced, not its whole
    history, and nothing older than the ledger's watermark: a restart owes
    the channel the outcome it is missing, not a replay of everything that
    ever happened. Repair states are read and never written.
    """
    watermark = since or notifier.log.watermark(utcnow())
    announced: list[str] = []
    for repair in sorted(repairs, key=lambda row: int(row["id"])):
        try:
            if str(repair.get("updated_at") or "") < watermark:
                continue
            message = message_for_state(
                repair, incident_of(int(repair["incident_id"]))
            )
            if message is None or notifier.log.get(message[0]) is not None:
                continue
            notifier.publish(
                *message,
                repair_id=int(repair["id"]),
                simulated_record=bool(repair.get("simulated")),
            )
            announced.append(message[0])
        except Exception:  # noqa: BLE001 - a status message may not break a repair
            log.exception("could not reconcile notifications for a repair")
    return announced


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


@dataclass(frozen=True)
class Recording:
    """A capture that states, itself, what it recorded.

    The head a clip was taken at is a property of the capture, never of the
    repair it is being attached to: inferring one from the other turns any
    supplied link into footage of an accepted fix. The recorder supplies the
    case, the full revision it ran against and when it was taken, and those
    are what the message quotes.
    """

    url: str
    case: str = ""
    sha: str = ""
    recorded_at: str = ""
    scope: str = ""


def recording_problem(recording: Recording, repair: dict[str, Any]) -> str:
    """Why this capture cannot be shown as footage of this head, if it cannot.

    Signed download URLs are refused because their query string is a bearer
    credential, and an incomplete or differing capture is named pending
    rather than published under the accepted head.
    """
    url = recording.url
    if not url.startswith("https://") or _is_signed(url):
        return "not a shareable https link"
    missing = [
        name
        for name, value in (
            ("case", recording.case),
            ("revision", recording.sha),
            ("capture time", recording.recorded_at),
        )
        if not value.strip()
    ]
    if missing:
        return f"the capture states no {', no '.join(missing)}"
    sha = recording.sha.strip().lower()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        return "the capture's revision is not a full commit sha"
    head = str(repair.get("pr_head_sha") or "").lower()
    if not head:
        return "the repair has no recorded pull request head"
    if sha != head:
        return f"the capture ran against {sha[:12]}, not the head {head[:12]}"
    return ""


def _video(repair: dict[str, Any], recording: Recording | None) -> str:
    """A recording line only when the capture itself says it shows this head.

    Nothing in the lifecycle records video, so this is empty unless an
    operator supplies a capture: an invented or unrelated recording would be
    worse than none. A capture that does not bind itself to the accepted head
    is reported as pending, with the reason, instead of being presented as
    verified footage.
    """
    if recording is None or not recording.url:
        return ""
    problem = recording_problem(recording, repair)
    if problem:
        return f" · recording pending — {_short(problem, 120)}"
    return (
        f" · recording {escape(recording.url)} — {escape(recording.case)} "
        f"captured {escape(recording.recorded_at)} against "
        f"`{escape(recording.sha.strip().lower()[:12])}`, shows "
        f"{_short(recording.scope or 'scope unstated', 120)}"
    )


#: Query parameters that make a URL a bearer credential rather than a link.
SIGNED_MARKERS = (
    "x-amz-signature",
    "x-goog-signature",
    "signature=",
    "token=",
    "sig=",
    "expires=",
)


def _is_signed(url: str) -> bool:
    query = url.partition("?")[2].lower()
    return any(marker in query for marker in SIGNED_MARKERS)


def _fingerprint(text: Any) -> str:
    return sha256(str(text or "").encode("utf-8")).hexdigest()[:12]


def historical_message(
    repair: dict[str, Any],
    incident: dict[str, Any] | None,
    attempt: dict[str, Any] | None,
    recording: Recording | None = None,
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
        f"{tail} {_links(repair)}{_video(repair, recording)}"
    )
    return f"backfill:{repair['id']}", "historical", text


def historical_parents(
    repairs: Sequence[tuple[dict[str, Any], dict[str, Any] | None]],
) -> tuple[dict[int, str], list[str]]:
    """Match each verified historical result to the repair it spoke for.

    A parent `ts` is adopted only where exactly one stored repair carries the
    same case and the same accepted head the operator confirmed in the
    channel. Anything else — no match, or several — is reported rather than
    guessed, because attaching a conversation to the wrong repair would reply
    to S2's result under S1's.
    """
    parents: dict[int, str] = {}
    problems: list[str] = []
    for known in HISTORICAL_THREADS:
        matched = [
            repair
            for repair, incident in repairs
            if _case(repair, incident) == known.case
            and str(repair.get("pr_head_sha") or "").lower() == known.sha
            and not repair.get("simulated")
        ]
        if len(matched) != 1:
            problems.append(
                f"{known.case} at {known.sha[:12]} matches "
                f"{len(matched)} stored repairs"
            )
            continue
        parents[int(matched[0]["id"])] = known.parent_ts
    return parents, problems


def result_problem(
    repair: dict[str, Any], attempt: dict[str, Any] | None
) -> str:
    """Why this attempt cannot speak for this repair's head, if it cannot.

    A result message names an accepted SHA, so the attempt it quotes has to
    be a passing one measured on exactly that SHA. An attempt from another
    head is a different experiment, and a simulated one measured nothing.
    """
    if attempt is None:
        return "no passing verification attempt is stored for this repair"
    if repair.get("simulated") or attempt.get("simulated"):
        return "the repair or its verification is simulated"
    if str(attempt.get("verdict") or "") != "passed":
        return f"the attempt did not pass (verdict {attempt.get('verdict')})"
    head = str(repair.get("pr_head_sha") or "")
    candidate = str(attempt.get("candidate_sha") or "")
    if not head:
        return "the repair has no recorded pull request head"
    if candidate != head:
        return (
            f"the attempt measured {candidate[:12]}, "
            f"not the repair's head {head[:12]}"
        )
    if str(repair.get("state") or "") != VERIFIED:
        return f"the repair is in state {repair.get('state')}, not {VERIFIED}"
    return ""


def result_message(
    repair: dict[str, Any],
    incident: dict[str, Any] | None,
    attempt: dict[str, Any] | None,
    *,
    recording: Recording | None = None,
) -> tuple[str, str, str]:
    """The final result for an accepted head, with its recording or without.

    Separate from the lifecycle and backfill messages, and keyed by the head
    and the recording itself, so attaching a replay that did not exist when
    the result was first announced publishes once and only once, while
    re-running the same command with the same link sends nothing. A result
    with no recording says so rather than implying footage exists.
    """
    problem = result_problem(repair, attempt)
    if problem:
        raise ValueError(problem)
    verified = dict(attempt or {})
    head = str(repair["pr_head_sha"])
    stem = (
        f"repair {repair['id']} ({_case(repair, incident)}), "
        f"incident {repair.get('incident_id')}"
    )
    video = _video(repair, recording)
    if not video:
        video = " · recording: none published for this head"
    return (
        f"{repair['id']}:result:{head}:"
        f"{_fingerprint(recording.url if recording else '')}",
        "result",
        f"Result — {stem}, tested head `{escape(head)}` "
        f"(verification {escape(str(verified.get('id') or ''))}, "
        f"cases {escape(str(verified.get('cases') or ''))}, "
        f"finished {escape(str(verified.get('finished_at') or ''))}). "
        f"{_reproduction(repair)}. {PREVIEW_ONLY}. {_links(repair)}{video}",
    )


def correction_message(text: str) -> tuple[str, str, str]:
    """A correction of something the channel was already told.

    Keyed by the correction's own wording, so the same correction sent twice
    is one message while a different one is a different event. It repairs the
    record forward; nothing in the channel is edited or deleted.
    """
    return f"correction:{_fingerprint(text)}", "correction", escape(text)


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


def token_from_environment() -> str:
    """The bot token, read only here, in the coordinator's own process.

    It is never placed in a prompt, an issue body, an event, a candidate
    container or a message, and no code path returns it to a caller.
    """
    return os.environ.get("SLACK_BOT_TOKEN", "").strip()


def build_notifier(
    config: Settings = settings,
    *,
    transport: Transport | None = None,
    bot: SlackBot | None = None,
    simulated: bool = False,
) -> Notifier:
    """The notifier this deployment gets: configured, or a recording no-op.

    The bot token is preferred and the webhook is the fallback: exactly one
    of them is live, because two configured transports would deliver every
    message twice.

    `simulated=True` refuses both ambient credentials outright. The channel
    is a production incident feed, so a scripted repair may reach it only
    through a transport the caller passes in deliberately, and says
    `[SIMULATED]` when it does.
    """
    token = "" if simulated else token_from_environment()
    bot = bot or (WebClientBot(token=token) if token else None)
    return Notifier(
        log=NotificationLog(config.data_dir / "notifications.sqlite"),
        webhook="" if bot is not None else webhook_from_environment(),
        bot=bot,
        transport=transport,
        simulated=simulated,
    )


def _state(config: Settings) -> Any:
    from .coordinator import open_state

    return open_state(config)


#: The one data directory the historical channel messages belong to. They
#: were posted about the repairs stored there, so bootstrapping them into any
#: other ledger would thread a different deployment's repairs under them.
LIVE_STATE_DIR = ("runtime", "live-state")


def _bootstrap(notifier: Notifier, config: Settings) -> int:
    """Adopt the operator-verified result messages as this ledger's parents.

    Explicit and one-time: a fresh state's first incident posts and adopts
    its own parent instead of replying under a result from another
    deployment's database.
    """
    where = config.data_dir.resolve()
    if (where.parent.name, where.name) != LIVE_STATE_DIR:
        print(
            json.dumps(
                {
                    "bootstrapped": {},
                    "reason": (
                        "the historical threads belong to "
                        f"{'/'.join(LIVE_STATE_DIR)}; this ledger is "
                        f"{where.parent.name}/{where.name}"
                    ),
                }
            )
        )
        return 2
    state = _state(config)
    repairs = [
        (repair, state.incidents.get(int(repair["incident_id"])))
        for repair in state.repairs.list()
    ]
    parents, problems = historical_parents(repairs)
    notifier.log.bootstrap_threads(notifier.channel, parents)
    print(
        json.dumps(
            {
                "bootstrapped": parents,
                "unmatched": problems,
                "threads": notifier.log.threads(),
            },
            indent=2,
        )
    )
    return 0 if parents and not problems else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Slack status notifications.")
    parser.add_argument(
        "command",
        choices=(
            "test",
            "bootstrap",
            "backfill",
            "result",
            "attach",
            "correction",
            "status",
        ),
    )
    parser.add_argument(
        "--clip",
        default="",
        help=(
            "path to an existing local recording to upload into the repair's "
            "thread; needs the recording metadata flags and a bot token"
        ),
    )
    parser.add_argument(
        "--text",
        default="",
        help="the correction to publish, verbatim; required for correction",
    )
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
    parser.add_argument(
        "--recording-scope",
        default="",
        help=(
            "what the recording actually shows, sent with the link: a console "
            "walk-through on the unfixed baseline is not footage of a repair"
        ),
    )
    parser.add_argument(
        "--recording-case",
        default="",
        help="the case the capture ran, as stated by whoever recorded it",
    )
    parser.add_argument(
        "--recording-sha",
        default="",
        help=(
            "the full commit sha the capture ran against; it is compared to "
            "the accepted head and never inferred from it"
        ),
    )
    parser.add_argument(
        "--recording-at",
        default="",
        help="when the capture was taken, in UTC",
    )
    args = parser.parse_args(argv)
    recording = (
        Recording(
            url=args.recording,
            case=args.recording_case,
            sha=args.recording_sha,
            recorded_at=args.recording_at,
            scope=args.recording_scope,
        )
        if args.recording
        else None
    )

    notifier = build_notifier(settings)
    if args.command == "status":
        print(
            json.dumps(
                {
                    "transport": notifier.transport_name,
                    "channel": notifier.channel,
                    "webhook_configured": bool(notifier.webhook),
                    "problem": notifier.problem,
                    "totals": notifier.log.totals(),
                    "threads": notifier.log.threads(),
                    "uploads": [
                        {
                            key: row[key]
                            for key in (
                                "upload_id",
                                "repair_id",
                                "sha",
                                "file_id",
                                "state",
                                "detail",
                            )
                        }
                        for row in notifier.log.uploads()
                    ],
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

    if args.command == "bootstrap":
        # Writes nothing to Slack, so it does not need a live transport.
        return _bootstrap(notifier, settings)

    if not notifier.enabled:
        print(
            json.dumps(
                {
                    "sent": False,
                    "reason": notifier.problem
                    or "no SLACK_BOT_TOKEN or SLACK_WEBHOOK_URL configured",
                }
            )
        )
        return 2

    if args.command == "test":
        event_id, kind, text = test_message(settings.run_id)
        print(json.dumps({"event_id": event_id, "state": notifier.publish(event_id, kind, text)}))
        return 0

    if args.command == "correction":
        if not args.text.strip():
            raise SystemExit("correction needs --text")
        event_id, kind, text = correction_message(args.text.strip())
        print(json.dumps({"event_id": event_id, "state": notifier.publish(event_id, kind, text)}))
        return 0

    if not args.repair:
        raise SystemExit(f"{args.command} needs at least one --repair <id>")
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
        if args.command == "attach":
            if recording is None or not args.clip:
                raise SystemExit("attach needs --clip and the recording metadata")
            clip = Path(args.clip)
            outcome = notifier.attach(
                clip_upload_id(repair_id, recording, clip),
                clip,
                recording,
                repair,
                attempts[-1] if attempts else None,
                simulated_record=bool(repair.get("simulated")),
            )
            results.append({"repair": repair_id, **outcome})
            continue
        if args.command == "result":
            passed = attempts[-1] if attempts else None
            problem = result_problem(repair, passed)
            if problem:
                results.append({"repair": repair_id, "state": "refused", "reason": problem})
                continue
            event_id, kind, text = result_message(
                repair, incident, passed, recording=recording
            )
            results.append(
                {
                    "repair": repair_id,
                    "event_id": event_id,
                    "tested_head": repair["pr_head_sha"],
                    "recording": (
                        recording_problem(recording, repair) or "published"
                        if recording
                        else "none supplied"
                    ),
                    "state": notifier.publish(
                        event_id,
                        kind,
                        text,
                        repair_id,
                        simulated_record=bool(repair.get("simulated")),
                    ),
                }
            )
            continue
        event_id, kind, text = historical_message(
            repair, incident, attempts[-1] if attempts else None, recording
        )
        results.append(
            {
                "repair": repair_id,
                "event_id": event_id,
                "state": notifier.publish(
                    event_id,
                    kind,
                    text,
                    repair_id,
                    simulated_record=bool(repair.get("simulated")),
                ),
            }
        )
    print(json.dumps({args.command: results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
