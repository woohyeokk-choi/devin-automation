"""One roll-up of a run, computed from the rows that already exist.

The console has always been able to answer "what happened to this incident?";
this answers "what happened in this run?" without the reader opening every
incident. Nothing is stored for it and no value is estimated: every number
below is a count of durable rows, and anything the data cannot support is
reported as unknown rather than filled in.

Three rules keep the counts honest:

* **A retry is not an outcome.** Repairs are counted as rows and preview or
  post-merge passes as *distinct repairs*, so a second verification attempt of
  the same repair cannot turn one success into two.
* **Persisted state is not live health.** A stored state stays whatever it was
  when it was last written. The freshness line reports when that happened and
  says so when nothing has been written recently; it never asserts that a
  coordinator is running or stopped, which this process cannot observe.
* **Reported consumption is not measured consumption.** A session that reports
  no ACUs is reported as unknown, never as free, and always separately from
  the limit the create request asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .controller import AWAITING_MERGE, MERGED, NEEDS_ATTENTION, TERMINAL, VERIFIED
from .validator import PASSED
from .verification import POST_MERGE, PREVIEW

#: How long a run may go without any recorded state change before the console
#: says so. A poll writes `updated_at` on every pass, so silence longer than
#: this means nobody is advancing this run — not that a process died, which is
#: a different claim needing different evidence.
STALE_AFTER = timedelta(minutes=30)

#: States that are still expected to move. A run whose repairs are all in one
#: of the settled states is quiet because it is finished, not because it is
#: stuck, so staleness is not reported for it.
SETTLED = frozenset({TERMINAL, MERGED})


@dataclass(frozen=True)
class RunSummary:
    """Counts for one run's state directory."""

    run: str
    incidents: int = 0
    repairs: int = 0
    states: tuple[tuple[str, int], ...] = ()
    linked_prs: int = 0
    preview_passed: int = 0
    merge_verified: int = 0
    awaiting_merge: int = 0
    attention: int = 0
    verification_attempts: int = 0
    unsuccessful_verifications: int = 0
    processing_errors: int = 0
    undelivered_notifications: int = 0
    requested_acus: int = 0
    reported_acus: str = "unknown"
    last_update_utc: str = ""
    quiet_for: str = ""
    stale: bool = False
    deadlines_elapsed: int = 0
    open_work: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


def _passed_repairs(attempts: Iterable[Mapping[str, Any]], stage: str) -> set[int]:
    """Distinct repairs with a real pass at this stage.

    Simulated attempts are excluded here for the same reason the console
    excludes them elsewhere: a rehearsal never counts as evidence.
    """
    return {
        int(attempt["repair_id"])
        for attempt in attempts
        if str(attempt.get("verdict") or "") == PASSED
        and str(attempt.get("stage") or PREVIEW) == stage
        and not int(attempt.get("simulated") or 0)
    }


def _parse(timestamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _elapsed(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def summarise(
    *,
    run: str,
    incidents: Sequence[Mapping[str, Any]],
    repairs: Sequence[Mapping[str, Any]],
    attempts: Sequence[Mapping[str, Any]],
    processing_errors: Sequence[Mapping[str, Any]] = (),
    notification_totals: Mapping[str, int] | None = None,
    now: datetime | None = None,
    stale_after: timedelta = STALE_AFTER,
) -> RunSummary:
    """Fold the stored rows of one run into the console's summary card."""
    moment = now or datetime.now(timezone.utc)
    states: dict[str, int] = {}
    for repair in repairs:
        state = str(repair.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1

    reported = sum(float(repair.get("agent_acus") or 0.0) for repair in repairs)
    stamps = [
        parsed
        for parsed in (_parse(str(repair.get("updated_at") or "")) for repair in repairs)
        if parsed is not None
    ]
    latest = max(stamps) if stamps else None
    open_work = sum(count for state, count in states.items() if state not in SETTLED)
    quiet = moment - latest if latest else None
    stale = bool(quiet and quiet > stale_after and open_work)

    elapsed = 0
    for repair in repairs:
        deadline = _parse(str(repair.get("deadline_utc") or ""))
        if deadline and deadline < moment and str(repair.get("state")) not in SETTLED:
            elapsed += 1

    notes: list[str] = []
    if stale and quiet:
        notes.append(
            f"No state change has been recorded for {_elapsed(quiet)}. Every value "
            "here is the last stored snapshot of this run, not a live reading; "
            "whether anything is still running is not something this page can see."
        )
    if elapsed:
        notes.append(
            f"{elapsed} repair(s) are past the deadline recorded at dispatch. A "
            "repair waiting for a human merge is expected to outlive it; the "
            "deadline bounds the session's own work, and is never extended here."
        )
    if reported <= 0 and any(repair.get("session_id") for repair in repairs):
        notes.append(
            "The API reported no consumption for the session(s) in this run, so "
            "usage is unknown. It is not evidence that the work was free."
        )

    return RunSummary(
        run=run or "(production)",
        incidents=len(incidents),
        repairs=len(repairs),
        states=tuple(sorted(states.items())),
        linked_prs=len(
            {str(repair.get("agent_pr_url")) for repair in repairs if repair.get("agent_pr_url")}
        ),
        preview_passed=len(_passed_repairs(attempts, PREVIEW)),
        merge_verified=len(_passed_repairs(attempts, POST_MERGE)),
        awaiting_merge=states.get(AWAITING_MERGE, 0) + states.get(VERIFIED, 0),
        attention=states.get(NEEDS_ATTENTION, 0) + states.get(TERMINAL, 0),
        verification_attempts=len(attempts),
        unsuccessful_verifications=sum(
            1 for attempt in attempts if str(attempt.get("verdict") or "") != PASSED
        ),
        processing_errors=len(processing_errors),
        undelivered_notifications=sum(
            count
            for state, count in (notification_totals or {}).items()
            if state in ("failed", "unknown")
        ),
        requested_acus=sum(int(repair.get("acu_limit") or 0) for repair in repairs),
        reported_acus="unknown" if reported <= 0 else f"{reported:g}",
        last_update_utc=latest.isoformat(timespec="seconds") if latest else "",
        quiet_for=_elapsed(quiet) if quiet else "",
        stale=stale,
        deadlines_elapsed=elapsed,
        open_work=open_work,
        notes=tuple(notes),
    )
