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
    # The demo gate. Not customer authentication: it only proves the caller is
    # an invited demo user, so that no anonymous client can pick a profile,
    # mutate upstream state or mint events that an incident could be built on.
    demo_username: str = field(
        default_factory=lambda: _env("PORTAL_DEMO_USERNAME", "demo")
    )
    demo_password: str = field(
        default_factory=lambda: _env("PORTAL_DEMO_PASSWORD", "demo-local")
    )
    cookie_secret: str = field(
        default_factory=lambda: _env("PORTAL_COOKIE_SECRET", "portal-local-dev-secret")
    )
    auto_repair_enabled: bool = field(
        default_factory=lambda: _env("AUTO_REPAIR_ENABLED", "false").lower() == "true"
    )

    # --- incidents ---------------------------------------------------------
    target_repo: str = field(
        default_factory=lambda: _env("PORTAL_TARGET_REPO", "woohyeokk-choi/superset")
    )
    #: Which incident this environment's runs belong to. Only a preview or
    #: verification environment sets it, and only the operator can: parent
    #: scope is deployment configuration, never a browser-supplied field.
    parent_incident: str = field(
        default_factory=lambda: _env("PORTAL_PARENT_INCIDENT", "")
    )
    #: Where verification builds candidate stacks, and where it writes their
    #: reports. Both are empty by default: a deployment that has not been
    #: given somewhere to work gets no verifier rather than a guessed path.
    automation_dir: str = field(default_factory=lambda: _env("PORTAL_AUTOMATION_DIR", ""))
    verification_workspace: str = field(
        default_factory=lambda: _env("PORTAL_VERIFICATION_WORKSPACE", "")
    )
    verification_artifacts: str = field(
        default_factory=lambda: _env("PORTAL_VERIFICATION_ARTIFACTS", "")
    )
    #: The product SHA this deployment is supposed to be running. When set,
    #: evidence measured against any other SHA is recorded but never becomes
    #: dispatchable: we cannot ask for a repair of code we cannot pin.
    baseline_sha: str = field(default_factory=lambda: _env("PORTAL_BASELINE_SHA", ""))

    @property
    def db_path(self) -> Path:
        return self.data_dir / _env("PORTAL_DB_FILE", "events.sqlite")

    @property
    def provenance_path(self) -> Path:
        if self.provenance_override:
            return Path(self.provenance_override)
        return self.data_dir / self.provenance_file


settings = Settings()
