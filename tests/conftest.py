"""Test environment: no real upstream, no shared state directory.

`portal.app` reads its settings at import time, so the environment has to be
fixed before any test imports it. Nothing listens on port 1, which keeps every
upstream call a `blocked` outcome instead of an accidental live call.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("PORTAL_DATA_DIR", tempfile.mkdtemp(prefix="portal-tests-"))
os.environ.setdefault("SUPERSET_BASE_URL", "http://127.0.0.1:1")
os.environ.setdefault("SUPERSET_MCP_URL", "http://127.0.0.1:1/mcp")
os.environ.setdefault("PORTAL_ENVIRONMENT_KIND", "test")
os.environ.setdefault("PORTAL_RUN_ID", "test")
