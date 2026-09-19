"""Shared helpers for baseline reproduction scenarios.

Records a sanitized HTTP transcript (no cookies, tokens or secrets) and writes
`result.json` plus a human-readable `reproduction.md` per scenario.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "baseline"
# The Superset checkout under test: whatever this run was pointed at, with a
# sibling of this repository as the only fallback — no host-specific path.
SUPERSET_CHECKOUT = Path(
    os.environ.get("SUPERSET_CHECKOUT")
    or os.environ.get("SUPERSET_DIR")
    or REPO_ROOT.parent / "superset"
)

_REDACTED = "<redacted>"
_SENSITIVE_HEADERS = {"cookie", "set-cookie", "authorization", "x-csrftoken"}


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(SUPERSET_CHECKOUT), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # pragma: no cover - diagnostics only
        return "unknown"


def source_revision() -> dict[str, str]:
    return {
        "superset_checkout": str(SUPERSET_CHECKOUT),
        "superset_sha": _git("rev-parse", "HEAD"),
        "superset_describe": _git("describe", "--tags", "--always"),
        "superset_dirty": "yes" if _git("status", "--porcelain") else "no",
        "automation_sha": subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        or "uncommitted",
    }


def sanitize_headers(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in dict(headers).items():
        out[key] = _REDACTED if key.lower() in _SENSITIVE_HEADERS else str(value)
    return out


class Transcript:
    """Collects sanitized request/response pairs."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, label: str, response: Any, request_body: Any = None) -> Any:
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text[:500]
        self.entries.append(
            {
                "step": label,
                "request": {
                    "method": response.request.method,
                    "url": response.request.url,
                    "headers": sanitize_headers(response.request.headers),
                    "body": request_body,
                },
                "response": {
                    "status": response.status_code,
                    "headers": sanitize_headers(response.headers),
                    "body": body,
                },
            }
        )
        return body


def write_artifacts(
    scenario_id: str,
    result: dict[str, Any],
    transcript: Transcript,
    markdown: str,
) -> Path:
    directory = ARTIFACT_ROOT / scenario_id
    directory.mkdir(parents=True, exist_ok=True)
    result = {
        "scenario_id": scenario_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "revision": source_revision(),
        **result,
    }
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    (directory / "transcript.json").write_text(
        json.dumps(transcript.entries, indent=2) + "\n"
    )
    (directory / "reproduction.md").write_text(markdown)
    return directory
