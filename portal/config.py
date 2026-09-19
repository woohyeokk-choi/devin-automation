"""Portal configuration.

Every path and endpoint is environment-driven so the same image runs on a
developer box, in the baseline Compose project, and later in an isolated
PR-verification project. No host-specific paths are baked in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


@dataclass(frozen=True)
class Settings:
    # --- upstream (server-side only; never taken from the browser) ---------
    superset_base_url: str = field(
        default_factory=lambda: _env("SUPERSET_BASE_URL", "http://localhost:8088")
    )
    mcp_url: str = field(
        default_factory=lambda: _env("SUPERSET_MCP_URL", "http://localhost:5008/mcp")
    )
    upstream_username: str = field(
        default_factory=lambda: _env("SUPERSET_USERNAME", "admin")
    )
    upstream_password: str = field(
        default_factory=lambda: _env("SUPERSET_PASSWORD", "admin")
    )
    restricted_username: str = field(
        default_factory=lambda: _env("SUPERSET_RESTRICTED_USERNAME", "restricted_analyst")
    )
    restricted_password: str = field(
        default_factory=lambda: _env("SUPERSET_RESTRICTED_PASSWORD", "restricted-analyst-local")
    )

    # --- fixtures ----------------------------------------------------------
    dataset_table: str = field(
        default_factory=lambda: _env("PORTAL_DATASET_TABLE", "synthetic_orders")
    )
    chart_name: str = field(
        default_factory=lambda: _env("PORTAL_CHART_NAME", "Synthetic orders by region")
    )

    # --- local state -------------------------------------------------------
    data_dir: Path = field(
        default_factory=lambda: Path(_env("PORTAL_DATA_DIR", "./runtime")).resolve()
    )
    provenance_file: str = field(
        default_factory=lambda: _env("PORTAL_PROVENANCE_FILE", "provenance.json")
    )
    provenance_override: str = field(
        default_factory=lambda: _env("PORTAL_PROVENANCE_PATH", "")
    )

    # --- identity ----------------------------------------------------------
    environment_kind: str = field(
        default_factory=lambda: _env("PORTAL_ENVIRONMENT_KIND", "baseline-light")
    )
    run_id: str = field(default_factory=lambda: _env("PORTAL_RUN_ID", "local"))
    ops_username: str = field(
        default_factory=lambda: _env("PORTAL_OPS_USERNAME", "operator")
    )
    ops_password: str = field(
        default_factory=lambda: _env("PORTAL_OPS_PASSWORD", "operator-local")
    )
    auto_repair_enabled: bool = field(
        default_factory=lambda: _env("AUTO_REPAIR_ENABLED", "false").lower() == "true"
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / _env("PORTAL_DB_FILE", "events.sqlite")

    @property
    def provenance_path(self) -> Path:
        if self.provenance_override:
            return Path(self.provenance_override)
        return self.data_dir / self.provenance_file


settings = Settings()
