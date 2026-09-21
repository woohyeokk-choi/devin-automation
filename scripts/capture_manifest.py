#!/usr/bin/env python3
"""Capture the fixture/config manifest for the baseline reproductions.

Probes the running light stack for the facts the scenarios depend on (Python
version, effective config path, cache backends, revisions, fixture row counts)
and writes `artifacts/baseline/manifest.json`.

Usage:
    python3 scripts/capture_manifest.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scenarios"))

from _harness import ARTIFACT_ROOT, source_revision  # noqa: E402

# Compose names containers after its project, so an isolated namespace is
# reached by exporting COMPOSE_PROJECT_NAME, never by editing this file.
COMPOSE_PROJECT = (
    os.environ.get("SUPERSET_COMPOSE_PROJECT")
    or os.environ.get("COMPOSE_PROJECT_NAME")
    or "superset"
)
SUPERSET_DIR = os.environ.get("SUPERSET_DIR", "<SUPERSET_DIR>")
AUTOMATION_DIR = os.environ.get(
    "AUTOMATION_DIR", str(Path(__file__).resolve().parents[1])
)
WEB_CONTAINER = f"{COMPOSE_PROJECT}-superset-light-1"
MCP_CONTAINER = f"{COMPOSE_PROJECT}-superset-mcp-light-1"
DB_CONTAINER = f"{COMPOSE_PROJECT}-db-light-1"

PROBE = """
import json, os
from superset.app import create_app
app = create_app()
with app.app_context():
    def describe(cfg):
        if isinstance(cfg, dict):
            return {k: (v if isinstance(v, (str, int, float, bool, type(None))) else type(v).__name__)
                    for k, v in cfg.items()}
        return type(cfg).__name__
    print(json.dumps({
        "superset_config_path": os.environ.get("SUPERSET_CONFIG_PATH"),
        "cache_config": describe(app.config["CACHE_CONFIG"]),
        "data_cache_config": describe(app.config["DATA_CACHE_CONFIG"]),
        "explore_form_data_cache_config": describe(app.config["EXPLORE_FORM_DATA_CACHE_CONFIG"]),
        "filter_state_cache_config": describe(app.config["FILTER_STATE_CACHE_CONFIG"]),
        "results_backend": type(app.config["RESULTS_BACKEND"]).__name__,
    }))
"""


def docker(container: str, *cmd: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["docker", "exec", "-i", container, *cmd],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def main() -> int:
    probe = json.loads(docker(WEB_CONTAINER, "python", "-", stdin=PROBE).splitlines()[-1])
    rows = docker(
        DB_CONTAINER,
        "psql",
        "-U",
        "superset",
        "-d",
        "superset_light",
        "-t",
        "-c",
        "SELECT count(*) FROM public.synthetic_orders;",
    ).strip()

    manifest = {
        "stack": {
            "compose_files": [
                f"{SUPERSET_DIR}/docker-compose-light.yml",
                f"{AUTOMATION_DIR}/stack/docker-compose.ports.yml",
            ],
            "compose_project": COMPOSE_PROJECT,
            "services": ["db-light", "superset-light", "superset-mcp-light"],
            "build_target": "dev (SUPERSET_BUILD_TARGET default)",
            "image": f"{COMPOSE_PROJECT}-superset-light:latest",
            "frontend_build_started": False,
            "redis": "not used by the light stack",
            # the host's view; the portal container uses the service names
            "superset_url": os.environ.get(
                "SUPERSET_BASE_URL", "http://127.0.0.1:8088"
            ),
            "mcp_url": os.environ.get(
                "SUPERSET_MCP_URL", "http://127.0.0.1:5008/mcp"
            ),
        },
        "runtime": {
            "container_python": docker(WEB_CONTAINER, "python", "-V"),
            "mcp_container_python": docker(MCP_CONTAINER, "python", "-V"),
            "fastmcp": docker(MCP_CONTAINER, "python", "-c", "import fastmcp;print(fastmcp.__version__)"),
            **probe,
        },
        "fixtures": {
            "database_connection": "Synthetic Analytics",
            "table": "public.synthetic_orders",
            "rows": int(rows),
            "dataset_columns": [
                "id",
                "order_ts",
                "region",
                "channel",
                "product",
                "units",
                "revenue",
            ],
            "seed_command": "python3 scripts/seed_synthetic.py --reset",
            "restricted_user": "restricted_analyst (role Gamma, created by the N1 scenario)",
        },
        "commands": {
            "start_stack": (
                f'cd "{SUPERSET_DIR}" && docker compose '
                "-f docker-compose-light.yml "
                f'-f "{AUTOMATION_DIR}/stack/docker-compose.ports.yml" '
                "up -d superset-light superset-mcp-light"
            ),
            "seed": (
                f'cd "{AUTOMATION_DIR}" && '
                "python3 scripts/seed_synthetic.py --reset"
            ),
            "s2": "python3 scenarios/s2_form_data_key_reuse.py",
            "s1": "python3 scenarios/s1_mcp_update_resets_fields.py",
            "n1": "python3 scenarios/n1_permission_denied_control.py",
            "manifest": "python3 scripts/capture_manifest.py",
        },
        "revision": source_revision(),
        "verification_note": (
            "docker-compose-light.yml bind-mounts ./superset and ./docker into the "
            "container, so an image rebuilt while the baseline source is mounted "
            "still executes baseline code. PR verification must run against an "
            "isolated checkout of the exact PR SHA (separate clone + separate "
            "compose project) or with those mounts removed; the revision block "
            "above records which source was actually executed."
        ),
        "auto_repair_enabled": False,
    }

    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_ROOT / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
