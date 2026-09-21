"""Test environment: no real upstream, no shared state directory.

`portal.app` reads its settings at import time, so the environment has to be
fixed before any test imports it. Nothing listens on port 1, which keeps every
upstream call a `blocked` outcome instead of an accidental live call.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

os.environ.setdefault("PORTAL_DATA_DIR", tempfile.mkdtemp(prefix="portal-tests-"))
os.environ.setdefault("SUPERSET_BASE_URL", "http://127.0.0.1:1")
os.environ.setdefault("SUPERSET_MCP_URL", "http://127.0.0.1:1/mcp")
os.environ.setdefault("PORTAL_ENVIRONMENT_KIND", "test")
os.environ.setdefault("PORTAL_RUN_ID", "test")

# Belt and braces around the runtime refusal in `build_notifier`: the suite
# runs in a shell that may hold the real webhook, and a test has no business
# posting into the incident channel even if some future wiring forgets to
# declare itself simulated.
os.environ.pop("SLACK_WEBHOOK_URL", None)

from portal.controller import RepairStore  # noqa: E402
from portal.incidents import IncidentStore  # noqa: E402

if TYPE_CHECKING:
    from test_controller import Wiring

#: The repository every simulated run is about.
REPO = "woohyeokk-choi/superset"


@pytest.fixture()
def incident(tmp_path: Path) -> dict[str, Any]:
    from test_controller import event

    store = IncidentStore(tmp_path / "incidents.sqlite", target_repo=REPO)
    store.observe(event(event_id="e1"))
    found = store.get(1)
    assert found is not None
    return found


@pytest.fixture()
def repairs(tmp_path: Path) -> RepairStore:
    # An isolated simulation database: simulated runs cannot touch real rows.
    return RepairStore(tmp_path / "simulated-repairs.sqlite", simulated=True)


@pytest.fixture()
def wiring(repairs: RepairStore) -> "Wiring":
    from test_controller import Wiring

    return Wiring(repairs)
