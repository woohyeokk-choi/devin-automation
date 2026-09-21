"""Canary test: planted secrets must not survive into SQLite, stdout or export.

The canaries sit exactly where a header-only sanitizer fails — nested request
input, nested upstream output, and exception text — and the assertion is made
against all three output channels produced from the same record.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from portal.events import EventStore
from portal.redaction import (
    WITHHELD,
    SafeError,
    pick,
    safe_error,
    safe_error_text,
    scrub,
    scrub_text,
)
from portal.tracing import Trace

CANARIES = [
    "s3cret-cookie-value",
    "s3cret-bearer-value",
    "s3cret-csrf-value",
    "s3cret-password-value",
    "s3cret-uri-password",
    "s3cret-apikey-value",
    "canary-credential-12345",
]

# Inline credential shapes that a scheme-unaware `key: value` rule redacts in
# the wrong order, publishing the credential and hiding only the scheme name.
INLINE_CREDENTIAL_TEXTS = [
    "Authorization: Bearer canary-credential-12345",
    "Authorization: Basic canary-credential-12345",
    "authorization=bearer canary-credential-12345",
    'headers={"Authorization": "Bearer canary-credential-12345"}',
    "Proxy-Authorization: Basic canary-credential-12345",
    "retrying with Bearer canary-credential-12345",
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
    for text in INLINE_CREDENTIAL_TEXTS:
        try:
            raise RuntimeError(text)
        except RuntimeError as exc:
            trace.blocked("superset.login", exc)
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
    assert exported[1]["output"]["error"]["error_type"] == "RuntimeError"


@pytest.mark.parametrize("text", INLINE_CREDENTIAL_TEXTS)
def test_scheme_prefixed_credentials_are_redacted_not_reordered(text: str) -> None:
    """The credential, not just the scheme name, must disappear."""
    scrubbed = scrub_text(text)
    assert "canary-credential-12345" not in scrubbed
    assert "<redacted>" in scrubbed


def test_arbitrary_exception_text_is_never_logged() -> None:
    detail = safe_error(ValueError("Authorization: Bearer canary-credential-12345"))
    assert detail == {"error_type": "ValueError", "message": WITHHELD}
    assert "canary" not in safe_error_text(ValueError("canary-credential-12345"))


def test_portal_errors_keep_their_own_structured_detail() -> None:
    class Boom(SafeError):
        def safe_detail(self) -> dict[str, str]:
            return {"service": "superset", "cause_type": "ConnectionError"}

    detail = safe_error(Boom("superset did not complete login (ConnectionError)"))
    assert detail["service"] == "superset"
    assert detail["message"].startswith("superset did not complete login")


def test_allowlist_drops_unknown_fields() -> None:
    picked = pick(
        {"key": "abc", "result": {"password": "s3cret-password-value"}},
        {"key", "message"},
    )
    assert picked == {"key": "abc"}


def test_portal_error_message_is_still_scrubbed() -> None:
    message = safe_error_text(SafeError("token=s3cret-apikey-value"))
    assert "s3cret-apikey-value" not in message
    assert message.startswith("SafeError:")
