#!/usr/bin/env python3
"""N1 (control) — a legitimate permission denial must stay a denial.

A restricted Gamma user, who has no access to the synthetic dataset, performs
the same explore action as S2. Superset must answer 403 and the pipeline must
classify the event as `expected_denial`: no incident, no issue, no repair.

Authorization and CSRF stay fully enabled; only a restricted user is created.

Run:
    python3 scenarios/n1_permission_denied_control.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from _harness import Transcript, write_artifacts  # noqa: E402
from superset_client import SupersetClient  # noqa: E402

SCENARIO_ID = "N1"
RESTRICTED_USER = "restricted_analyst"
# Throwaway credential for a Gamma user that exists only inside the local
# light stack; override with RESTRICTED_PASSWORD for any shared environment.
RESTRICTED_PASSWORD = os.environ.get("RESTRICTED_PASSWORD", "restricted-analyst-local")
TAB_ID = "552266"


def _fab(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", "-i", "superset-superset-light-1", "superset", "fab", *args],
        capture_output=True,
        text=True,
    )


def ensure_restricted_user() -> str:
    """Create the Gamma user in the light stack, or realign its password."""
    proc = _fab(
        "create-user",
        "--role",
        "Gamma",
        "--username",
        RESTRICTED_USER,
        "--firstname",
        "Restricted",
        "--lastname",
        "Analyst",
        "--email",
        f"{RESTRICTED_USER}@example.invalid",
        "--password",
        RESTRICTED_PASSWORD,
    )
    output = proc.stdout + proc.stderr
    if "already exists" in output or proc.returncode != 0:
        proc = _fab(
            "reset-password",
            "--username",
            RESTRICTED_USER,
            "--password",
            RESTRICTED_PASSWORD,
        )
        output = proc.stdout + proc.stderr
    tail = output.strip().splitlines()[-1:] or [""]
    return tail[0]


def main() -> int:
    admin = SupersetClient()
    admin.login()
    dataset_id = admin.find_dataset("synthetic_orders")
    if dataset_id is None:
        print("dataset missing — run scripts/seed_synthetic.py first", file=sys.stderr)
        return 2

    create_output = ensure_restricted_user()

    client = SupersetClient()
    client.login(RESTRICTED_USER, RESTRICTED_PASSWORD)
    transcript = Transcript()

    body = {
        "datasource_id": dataset_id,
        "datasource_type": "table",
        "form_data": json.dumps(
            {
                "datasource": f"{dataset_id}__table",
                "viz_type": "table",
                "groupby": ["region"],
                "metrics": ["count"],
                "row_limit": 100,
            }
        ),
    }
    resp = client.post(f"/api/v1/explore/form_data?tab_id={TAB_ID}", json=body)
    status = resp.status_code
    transcript.record("restricted user creates explore state", resp, body)

    resp2 = client.get("/api/v1/chart/?q=(page_size:1)")
    listing_status = resp2.status_code
    transcript.record("restricted user lists charts (allowed action)", resp2)

    denied = status in (401, 403, 404)
    result = {
        "title": "Expected permission denial stays denied and starts no repair",
        "is_control": True,
        "restricted_user": RESTRICTED_USER,
        "role": "Gamma",
        "dataset_id": dataset_id,
        "denial_status": status,
        "denied_as_expected": denied,
        "allowed_action_status": listing_status,
        "classification": "expected_denial",
        "should_create_incident": False,
        "expected": "403 (or equivalent denial) with no incident and no repair path",
        "observed": f"HTTP {status} on the restricted explore write",
        "authorization_disabled": False,
        "csrf_disabled": False,
        "user_creation_output": create_output,
    }

    markdown = f"""# N1 (control) — expected permission denial

Status: **{"PASS" if denied else "FAIL"}** — the denial is preserved.

## User action

`{RESTRICTED_USER}` (role `Gamma`, no access to the synthetic dataset) performs
the same explore-state write as S2.

## Steps

1. `POST /api/v1/explore/form_data?tab_id={TAB_ID}` as the restricted user →
   **HTTP {status}**
2. `GET /api/v1/chart/` as the same user (an action the role *is* entitled to) →
   HTTP {listing_status}

## Expected vs observed

- Expected: {result["expected"]}
- Observed: {result["observed"]}

## Pipeline contract

This event must be classified `expected_denial`: it is a correct authorization
outcome, not a defect. No incident is created, no issue is opened and no repair
session is started. Authorization and CSRF protection were left enabled for the
whole run — the only change was creating a low-privilege user.

## Evidence

- `transcript.json` — sanitized requests/responses (no cookies, no CSRF tokens).
- `result.json` — machine-readable result and source revision.
"""

    directory = write_artifacts(SCENARIO_ID, result, transcript, markdown)
    print(json.dumps({"denied_as_expected": denied, "status": status, "artifacts": str(directory)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
