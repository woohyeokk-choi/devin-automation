"""Sanitization applied *before* anything reaches SQLite, stdout or an export.

Two independent layers, because either one alone is unsafe:

1. **Allowlist** (`pick`) — only named fields of a request/response ever leave
   the process. Everything else is dropped, so a new upstream field cannot leak
   by default.
2. **Scrub** (`scrub`) — every surviving string, key name, nested structure and
   exception message is still swept for credential shapes, because error text
   and free-form bodies carry secrets no allowlist can anticipate.

Raw payloads are never stored: strings are truncated and containers are capped.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

REDACTED = "<redacted>"
DROPPED = "<dropped>"
MAX_STRING = 300
MAX_ITEMS = 25

# Key names whose *value* is always replaced, at any nesting depth.
SENSITIVE_KEYS = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-csrftoken",
        "csrf_token",
        "csrftoken",
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "x-api-key",
        "session",
        "session_id",
        "sqlalchemy_uri",
        "encrypted_extra",
        "masked_encrypted_extra",
        "dsn",
        "devin_api_key",
        "github_token",
    }
)

# An auth scheme keeps its credential in the *next* token, so a
# `key: value` rule that stops at the first whitespace would redact the scheme
# name and publish the credential behind it. Every rule below therefore
# consumes the scheme together with what follows it.
_SCHEME = r"(?:bearer|basic|digest|token|apikey)"

_SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # user:password@host in any connection URI
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@"),
        rf"\1{REDACTED}:{REDACTED}@",
    ),
    # `Authorization: Bearer <token>` and every other scheme-prefixed credential
    (
        re.compile(rf"(?i)\b{_SCHEME}\s+[^\s,;\"'}})\]]+"),
        REDACTED,
    ),
    # key=value / "key": "value" style credentials in free text and JSON blobs,
    # including a scheme-prefixed value
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|"
            r"refresh[_-]?token|csrf[_-]?token|token|cookie|authorization)\b"
            rf"(\"?\s*[:=]\s*\"?)(?:{_SCHEME}\s+)?([^\s,;&\"'}})\]]+)"
        ),
        rf"\1\2{REDACTED}",
    ),
    # An incoming webhook URL is a bearer credential in path form: anyone
    # holding it can post to the channel, and it carries no `key=value` shape
    # for the rules above to catch.
    (re.compile(r"(?i)https://hooks\.slack\.com/\S*"), REDACTED),
    # A Slack bot/user token is the same kind of bare bearer credential: it
    # carries no `key=value` shape and grants everything its scopes allow.
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{8,}"), REDACTED),
    # JWTs and Flask session cookies, which appear bare in logs and tracebacks
    (re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}"), REDACTED),
    (re.compile(r"\bsession=[^\s;,\"']+"), f"session={REDACTED}"),
)


def scrub_text(value: str) -> str:
    out = value
    for pattern, replacement in _SCRUBBERS:
        out = pattern.sub(replacement, out)
    if len(out) > MAX_STRING:
        out = out[:MAX_STRING] + "…"
    return out


def scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively redact credential-shaped content in arbitrary data."""
    if _depth > 6:
        return DROPPED
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:MAX_ITEMS]:
            key = str(raw_key)
            if key.lower() in SENSITIVE_KEYS:
                out[key] = REDACTED
            else:
                out[key] = scrub(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [scrub(item, _depth + 1) for item in list(value)[:MAX_ITEMS]]
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub_text(str(value))


def pick(data: Any, allowed: Iterable[str]) -> dict[str, Any]:
    """Keep only allowlisted top-level keys, then scrub what survives."""
    allowed = set(allowed)
    if not isinstance(data, Mapping):
        return {"_value": scrub(data)}
    return {key: scrub(value) for key, value in data.items() if key in allowed}


WITHHELD = "<withheld: message of an unrecognised error type>"


class SafeError(Exception):
    """An error whose message the portal wrote itself, so it may be logged.

    Pattern scrubbing of free-form text is a safety net, not a boundary: an
    arbitrary third-party exception can stringify a request URL with embedded
    credentials, a header dump or a response body in a shape no pattern
    anticipates. Only errors raised here carry their message into the log;
    everything else contributes its type and structured detail only.
    """

    def safe_detail(self) -> dict[str, Any]:
        return {}


def safe_error(exc: BaseException) -> dict[str, Any]:
    """Structured, allowlisted error metadata — never a raw exception string."""
    if isinstance(exc, SafeError):
        return {
            "error_type": type(exc).__name__,
            "message": scrub_text(str(exc)),
            **scrub(exc.safe_detail()),
        }
    return {"error_type": type(exc).__name__, "message": WITHHELD}


def safe_error_text(exc: BaseException) -> str:
    """One-line form of `safe_error`, for an event's `message` column."""
    detail = safe_error(exc)
    return f"{detail['error_type']}: {detail['message']}"
