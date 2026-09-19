#!/usr/bin/env python3
"""S2 — deleted explore form-data keys are reused.

User action (portal): open a chart in Explore in a browser tab, change the
filters (Superset stores the state under a key), discard that exploration
state, then start a new exploration in the same tab.

Expected: the discarded key stays dead; the new exploration gets a new key.
Observed at the baseline: the second exploration is handed the key that was
just deleted, so the dead key resurrects and resolves to the new state.

Run:
    python3 scenarios/s2_form_data_key_reuse.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scenarios._harness import Transcript, write_artifacts  # noqa: E402
from clients.superset_client import SupersetClient  # noqa: E402

SCENARIO_ID = "S2"
TAB_ID = "991177"
DATASET_TABLE = "synthetic_orders"

FORM_DATA_A = {
    "datasource": "{dataset_id}__table",
    "viz_type": "table",
    "granularity_sqla": "order_ts",
    "groupby": ["region"],
    "metrics": ["count"],
    "row_limit": 100,
    "adhoc_filters": [
        {
            "expressionType": "SIMPLE",
            "subject": "channel",
            "operator": "==",
            "comparator": "web",
            "clause": "WHERE",
        }
    ],
}

FORM_DATA_B = {
    "datasource": "{dataset_id}__table",
    "viz_type": "table",
    "granularity_sqla": "order_ts",
    "groupby": ["product"],
    "metrics": ["count"],
    "row_limit": 250,
    "adhoc_filters": [
        {
            "expressionType": "SIMPLE",
            "subject": "channel",
            "operator": "==",
            "comparator": "retail",
            "clause": "WHERE",
        }
    ],
}


def key_value_rows() -> list[str]:
    """Metastore evidence: entries in the key_value table for the form-data cache."""
    out = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "superset-db-light-1",
            "psql",
            "-U",
            "superset",
            "-d",
            "superset_light",
            "-t",
            "-c",
            "SELECT resource, uuid FROM key_value ORDER BY id;",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def main() -> int:
    client = SupersetClient()
    client.login()
    transcript = Transcript()

    dataset_id = client.find_dataset(DATASET_TABLE)
    if dataset_id is None:
        print("dataset missing — run scripts/seed_synthetic.py first", file=sys.stderr)
        return 2

    def payload(form_data: dict) -> dict:
        fd = dict(form_data)
        fd["datasource"] = fd["datasource"].format(dataset_id=dataset_id)
        return {
            "datasource_id": dataset_id,
            "datasource_type": "table",
            "form_data": json.dumps(fd),
        }

    body_a = payload(FORM_DATA_A)
    resp = client.post(f"/api/v1/explore/form_data?tab_id={TAB_ID}", json=body_a)
    k1 = transcript.record("create A (tab_id set)", resp, body_a)["key"]

    resp = client.get(f"/api/v1/explore/form_data/{k1}")
    transcript.record("read K1", resp)

    resp = client.delete(f"/api/v1/explore/form_data/{k1}")
    delete_status = resp.status_code
    transcript.record("delete K1 (no tab_id accepted by the route)", resp)

    resp = client.get(f"/api/v1/explore/form_data/{k1}")
    absence_status = resp.status_code
    transcript.record("read K1 after delete (expect 404)", resp)

    rows_after_delete = key_value_rows()

    body_b = payload(FORM_DATA_B)
    resp = client.post(f"/api/v1/explore/form_data?tab_id={TAB_ID}", json=body_b)
    k2 = transcript.record("create B (same tab_id)", resp, body_b)["key"]

    resp = client.get(f"/api/v1/explore/form_data/{k1}")
    k1_after_create_b = transcript.record("read K1 after create B", resp)

    reused = k1 == k2
    resurrected = k1_after_create_b.get("form_data") is not None

    result = {
        "title": "Deleted explore form-data key is reused by the next exploration",
        "reproduced": bool(reused and resurrected),
        "tab_id": TAB_ID,
        "dataset_id": dataset_id,
        "keys": {"k1": k1, "k2": k2, "reused": reused},
        "delete_status": delete_status,
        "absence_status_after_delete": absence_status,
        "k1_resurrected_after_create_b": resurrected,
        "expected": (
            "K2 is a fresh key; K1 stays deleted and keeps returning 404 "
            "regardless of later explorations in the same tab"
        ),
        "observed": (
            f"K2 == K1 ({reused}); after the second exploration the deleted key "
            f"resolves again (resurrected={resurrected}) and now serves the new "
            "exploration's form data"
        ),
        "mechanism": (
            "POST /api/v1/explore/form_data reads tab_id from the query string and "
            "stores a contextual mapping cache_key(session_id, tab_id, datasource_id, "
            "chart_id, datasource_type) -> key. DELETE /api/v1/explore/form_data/<key> "
            "builds CommandParameters(key=key) without tab_id, so the delete command "
            "deletes the contextual mapping for tab_id=None and leaves the real "
            "mapping in place. The next create finds that stale mapping and hands out "
            "the deleted key again."
        ),
        "code_paths": [
            "superset/explore/form_data/api.py::ExploreFormDataRestApi.delete",
            "superset/commands/explore/form_data/delete.py::DeleteFormDataCommand.run",
            "superset/commands/explore/form_data/create.py::CreateFormDataCommand.run",
            "superset/temporary_cache/utils.py::cache_key",
        ],
        "metastore_key_value_rows_after_delete": rows_after_delete,
        "cache_backend": "SupersetMetastoreCache (key_value table, Postgres)",
    }

    markdown = f"""# S2 — deleted explore form-data key is reused

Status: **{"REPRODUCED" if result["reproduced"] else "NOT REPRODUCED"}** at the
baseline commit (`{result.get("revision", {}).get("superset_sha", "")}` recorded in
`result.json`).

## User action

In the portal a user explores the synthetic `{DATASET_TABLE}` dataset in one
browser tab, discards that exploration state, then starts a new exploration in
the same tab.

## Steps (plain REST, one authenticated cookie session, non-empty `tab_id={TAB_ID}`)

1. `POST /api/v1/explore/form_data?tab_id={TAB_ID}` → key **K1** = `{k1}`
2. `GET /api/v1/explore/form_data/{{K1}}` → 200, exploration A
3. `DELETE /api/v1/explore/form_data/{{K1}}` → {delete_status}
4. `GET /api/v1/explore/form_data/{{K1}}` → {absence_status} (absence confirmed)
5. `POST /api/v1/explore/form_data?tab_id={TAB_ID}` → key **K2** = `{k2}`
6. `GET /api/v1/explore/form_data/{{K1}}` → resurrected = `{resurrected}`

## Expected vs observed

- Expected: {result["expected"]}
- Observed: {result["observed"]}

## Mechanism (hypothesis 1 — missing `tab_id` on DELETE, REST-only)

{result["mechanism"]}

The MCP session-id override (`MCPCreateFormDataCommand._get_session_id`) is a
**separate** hypothesis: it is not involved here, since this reproduction never
touches the MCP service and uses a single cookie session throughout.

## Evidence

- `transcript.json` — sanitized requests/responses (no cookies, no CSRF tokens).
- `result.json` — machine-readable result, keys, statuses and source revision.
- Cache backend: {result["cache_backend"]}.
"""

    directory = write_artifacts(SCENARIO_ID, result, transcript, markdown)
    print(json.dumps({"reproduced": result["reproduced"], "artifacts": str(directory)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
