"""Canary test: planted secrets must not survive into SQLite, stdout or export.

The canaries sit exactly where a header-only sanitizer fails — nested request
input, nested upstream output, and exception text — and the assertion is made
against all three output channels produced from the same record.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from portal.events import EventStore
from portal.redaction import pick, safe_exception, scrub
from portal.tracing import Trace

CANARIES = [
    "s3cret-cookie-value",
    "s3cret-bearer-value",
    "s3cret-csrf-value",
    "s3cret-password-value",
    "s3cret-uri-password",
    "s3cret-apikey-value",
]


def _trace(tmp_path: Path) -> tuple[Trace, EventStore, io.StringIO]:
    stream = io.StringIO()
    store = EventStore(tmp_path / "events.sqlite", stream=stream)
    trace = Trace(
        store=store,
        actor="analyst",
        environment_kind="test",
        run_id="test",
        revision={"strength": "unmeasured"},
    )
    return trace, store, stream


def test_canaries_never_reach_any_channel(tmp_path: Path) -> None:
    trace, store, stream = _trace(tmp_path)

    trace.log(
        "upstream_call",
        "superset.login",
        "ok",
        input=scrub(
            {
                "headers": {
                    "Cookie": "session=s3cret-cookie-value",
                    "Authorization": "Bearer s3cret-bearer-value",
                    "X-CSRFToken": "s3cret-csrf-value",
                },
                "body": {"credentials": {"password": "s3cret-password-value"}},
            }
        ),
        output=scrub(
            {
                "database": {
                    "sqlalchemy_uri": "postgresql://admin:s3cret-uri-password@db:5432/x",
                    "extra": {"api_key": "s3cret-apikey-value"},
                }
            }
        ),
    )
    try:
        raise RuntimeError(
            "connect failed for postgresql://admin:s3cret-uri-password@db:5432/x "
            "with api_key=s3cret-apikey-value"
        )
    except RuntimeError as exc:
        trace.blocked("superset.login", exc)

    channels = {
        "stdout": stream.getvalue(),
        "sqlite": (tmp_path / "events.sqlite").read_bytes().decode("utf-8", "replace"),
        "export": "".join(store.export_jsonl()),
    }
    for name, blob in channels.items():
        for canary in CANARIES:
            assert canary not in blob, f"{canary} leaked into {name}"

    # The event is still useful: the shape survives, only values are redacted.
    exported = [json.loads(line) for line in store.export_jsonl()]
    assert exported[0]["input"]["headers"]["Cookie"] == "<redacted>"
    assert exported[0]["output"]["database"]["sqlalchemy_uri"] == "<redacted>"
    assert "RuntimeError" in exported[1]["message"]


def test_allowlist_drops_unknown_fields() -> None:
    picked = pick(
        {"key": "abc", "result": {"password": "s3cret-password-value"}},
        {"key", "message"},
    )
    assert picked == {"key": "abc"}


def test_exception_text_is_scrubbed() -> None:
    message = safe_exception(ValueError("token=s3cret-apikey-value"))
    assert "s3cret-apikey-value" not in message
    assert message.startswith("ValueError:")
