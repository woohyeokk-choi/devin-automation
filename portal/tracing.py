"""Trace and request identity for one user action.

One user action = one `trace_id`. Every upstream REST/MCP call inside it gets a
`request_id` and a monotonically increasing `step_index`, so a trace reads as an
ordered story: user action → upstream calls → contract assertions → verdict.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .events import EventStore, utcnow
from .redaction import safe_exception, scrub


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Trace:
    store: EventStore
    actor: str
    environment_kind: str
    run_id: str
    revision: dict[str, Any]
    scenario: str | None = None
    trace_id: str = field(default_factory=lambda: new_id("trace"))
    _step: int = 0

    def _next_step(self) -> int:
        self._step += 1
        return self._step

    def log(
        self,
        kind: str,
        operation: str,
        outcome: str,
        *,
        request_id: str | None = None,
        http_status: int | None = None,
        tool_name: str | None = None,
        duration_ms: int | None = None,
        message: str | None = None,
        input: dict[str, Any] | None = None,  # noqa: A002 - log field name
        output: dict[str, Any] | None = None,
        assertion: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.store.emit(
            {
                "ts_utc": utcnow(),
                "trace_id": self.trace_id,
                "request_id": request_id,
                "step_index": self._next_step(),
                "kind": kind,
                "outcome": outcome,
                "scenario": self.scenario,
                "operation": operation,
                "actor": self.actor,
                "environment_kind": self.environment_kind,
                "run_id": self.run_id,
                "http_status": http_status,
                "tool_name": tool_name,
                "duration_ms": duration_ms,
                "message": scrub(message) if message else None,
                "input": scrub(input or {}),
                "output": scrub(output or {}),
                "assertion": scrub(assertion) if assertion else None,
                "revision": self.revision,
            }
        )

    # ------------------------------------------------------------ helpers
    def action(self, operation: str, **kw: Any) -> dict[str, Any]:
        return self.log("user_action", operation, kw.pop("outcome", "ok"), **kw)

    def assert_contract(
        self,
        name: str,
        expected: Any,
        observed: Any,
        *,
        operation: str,
        holds: bool | None = None,
        detail: str | None = None,
        subject: str = "product_contract",
        known_baseline_defect: bool = False,
    ) -> bool:
        """Record a semantic contract check.

        A violated contract is `assertion_failed` — an HTTP 200 carrying wrong
        state. It is never turned into a fabricated transport error.

        `subject` says whose contract broke: `product_contract` is Superset's
        behaviour, `harness` is the portal's own plumbing. A failure marked
        `known_baseline_defect` is the defect this environment exists to
        reproduce, so a report can separate "the baseline is broken as
        expected" from "the automation is broken".
        """
        ok = (expected == observed) if holds is None else bool(holds)
        self.log(
            "assertion",
            operation,
            "ok" if ok else "assertion_failed",
            message=detail,
            assertion={
                "name": name,
                "expected": expected,
                "observed": observed,
                "holds": ok,
                "subject": subject,
                "known_baseline_defect": known_baseline_defect,
            },
        )
        return ok

    def blocked(self, operation: str, exc: BaseException | None = None, message: str | None = None) -> None:
        """Environment/setup failure: never 'not reproduced', never 'verified'."""
        self.log(
            "lifecycle",
            operation,
            "blocked",
            message=message or (safe_exception(exc) if exc else "blocked"),
        )


class Timer:
    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.ms = int((time.perf_counter() - self._start) * 1000)
