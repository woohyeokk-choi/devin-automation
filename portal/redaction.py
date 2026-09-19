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

_SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # user:password@host in any connection URI
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@"),
        rf"\1{REDACTED}:{REDACTED}@",
    ),
    # key=value / "key": "value" style credentials in free text and JSON blobs
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|"
            r"refresh[_-]?token|csrf[_-]?token|token|cookie|authorization)\b"
            r"(\"?\s*[:=]\s*\"?)([^\s,;&\"'})\]]+)"
        ),
        rf"\1\2{REDACTED}",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), f"Bearer {REDACTED}"),
    (re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}"), f"Basic {REDACTED}"),
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


def safe_exception(exc: BaseException) -> str:
    return scrub_text(f"{type(exc).__name__}: {exc}")
