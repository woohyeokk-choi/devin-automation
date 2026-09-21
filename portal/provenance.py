"""What the portal knows about the code it is actually talking to.

Phase 1 reported `git rev-parse HEAD` of a configured host path, which proves
nothing about the process serving the requests. This module instead reads a
measurement file produced by `scripts/capture_provenance.py`, which inspects
the real container (Compose project/service, container id, image id, mount
table) and hashes the Python tree *inside* that container.

When no measurement is available the portal says so — `strength: "unmeasured"`
— rather than substituting a host-side git SHA.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

UNMEASURED: dict[str, Any] = {
    "strength": "unmeasured",
    "note": "run scripts/capture_provenance.py to measure the running container",
}


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return dict(UNMEASURED)
    if not isinstance(data, dict):
        return dict(UNMEASURED)
    return data


def summary(provenance: dict[str, Any]) -> dict[str, Any]:
    """The few fields stamped onto every event."""
    container = provenance.get("container") or {}
    source = provenance.get("source") or {}
    fixtures = provenance.get("fixtures") or {}
    return {
        "strength": provenance.get("strength", "unmeasured"),
        "compose_project": container.get("compose_project"),
        "compose_service": container.get("compose_service"),
        "container_id": container.get("id"),
        "image_id": container.get("image_id"),
        "running_code_sha256": source.get("running_code_sha256"),
        "checkout_sha": source.get("checkout_sha"),
        "checkout_matches_running_code": source.get("checkout_matches_running_code"),
        "fixture_revision": fixtures.get("revision"),
    }
