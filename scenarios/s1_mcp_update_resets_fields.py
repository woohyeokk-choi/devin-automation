#!/usr/bin/env python3
"""S1 — an MCP chart update resets settings the caller did not mention.

User action (portal): an assistant working through the Superset MCP service
saves a chart with an explicit colour scheme and row limit, then later asks for
one narrow change (here: sort the same table differently).

Expected: settings the update does not mention survive the update.
Observed at the baseline: `row_limit` falls back to the schema default of 1000.
`color_scheme` is checked in the same run, so the scenario reports per field
instead of assuming both reset.

Requires the MCP sidecar:
    docker compose -f docker-compose-light.yml \
      -f /home/ubuntu/repos/devin-automation/stack/docker-compose.ports.yml \
      up -d superset-mcp-light

Run:
    python3 scenarios/s1_mcp_update_resets_fields.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scenarios._harness import Transcript, write_artifacts  # noqa: E402
from clients.mcp_client import MCPClient  # noqa: E402
from clients.superset_client import SupersetClient  # noqa: E402

SCENARIO_ID = "S1"
CHART_NAME = "S1 synthetic orders by region"
COLOR_SCHEME = "googleCategory10c"
ROW_LIMIT = 137


def main() -> int:
    rest = SupersetClient()
    rest.login()
    dataset_id = rest.find_dataset("synthetic_orders")
    if dataset_id is None:
        print("dataset missing — run scripts/seed_synthetic.py first", file=sys.stderr)
        return 2

    stale_charts = rest.get(
        "/api/v1/chart/",
        params={
            "q": f"(filters:!((col:slice_name,opr:eq,value:'{CHART_NAME}')),page_size:100)"
        },
    ).json()["result"]
    for stale in stale_charts:
        rest.delete(f"/api/v1/chart/{stale['id']}")

    mcp = MCPClient()
    mcp.initialize()
    transcript = Transcript()
    calls: list[dict] = []

    def tool(name: str, arguments: dict) -> dict:
        # Chart tools take a single `request` object argument.
        result = mcp.call_tool(
            "call_tool", {"name": name, "arguments": {"request": arguments}}
        )
        calls.append({"tool": name, "arguments": arguments, "result": result})
        return result

    create_args = {
        "dataset_id": dataset_id,
        "chart_name": CHART_NAME,
        "save_chart": True,
        "generate_preview": False,
        "config": {
            "chart_type": "table",
            "columns": [{"name": "region"}, {"name": "revenue", "aggregate": "SUM"}],
            "row_limit": ROW_LIMIT,
            "color_scheme": COLOR_SCHEME,
        },
    }
    created = tool("generate_chart", create_args)
    chart_id = (created.get("chart") or {}).get("id")
    if not chart_id:
        print(json.dumps(created, indent=2)[:2000], file=sys.stderr)
        return 3

    resp = rest.get(f"/api/v1/chart/{chart_id}")
    params_before = json.loads(transcript.record("read chart after create", resp)["result"]["params"])

    update_args = {
        "identifier": chart_id,
        "generate_preview": False,
        "config": {
            "chart_type": "table",
            "columns": [{"name": "region"}, {"name": "revenue", "aggregate": "SUM"}],
            "sort_by": ["SUM(revenue)"],
        },
    }
    tool("update_chart", update_args)

    resp = rest.get(f"/api/v1/chart/{chart_id}")
    params_after = json.loads(transcript.record("read chart after update", resp)["result"]["params"])

    row_limit_before = params_before.get("row_limit")
    row_limit_after = params_after.get("row_limit")
    scheme_before = params_before.get("color_scheme")
    scheme_after = params_after.get("color_scheme")

    row_limit_reset = row_limit_before != row_limit_after
    scheme_reset = scheme_before != scheme_after
    reproduced = row_limit_reset or scheme_reset
    reset_fields = [
        name
        for name, flag in (
            ("row_limit", row_limit_reset),
            ("color_scheme", scheme_reset),
        )
        if flag
    ]

    result = {
        "title": "MCP chart update resets omitted color_scheme / row_limit",
        "reproduced": reproduced,
        "chart_id": chart_id,
        "dataset_id": dataset_id,
        "mcp_service": {
            "url": mcp.url,
            "transport": "streamable-http (stateless)",
            "started_by": (
                "stack/docker-compose.ports.yml service `superset-mcp-light`, "
                "image superset-superset-light, command "
                "`superset mcp run --host 0.0.0.0 --port 5008`"
            ),
            "dependency": (
                "fastmcp 3.4.7, present in the built image "
                "(Dockerfile `superset` target installs the fastmcp extra; the "
                "`dev` target gets it via requirements/development.txt)"
            ),
            "auth": "MCP_DEV_USERNAME=admin from stack/superset_config_mcp.py",
            "tool_access": (
                "tool search transform is on, so tools are invoked through the "
                "`call_tool` proxy"
            ),
        },
        "fields": {
            "row_limit": {
                "before": row_limit_before,
                "after": row_limit_after,
                "reset": row_limit_reset,
            },
            "color_scheme": {
                "before": scheme_before,
                "after": scheme_after,
                "reset": scheme_reset,
            },
        },
        "reset_fields": reset_fields,
        "expected": (
            "an update that only changes sort_by keeps row_limit "
            f"{ROW_LIMIT} and color_scheme '{COLOR_SCHEME}'"
        ),
        "observed": (
            f"row_limit {row_limit_before} -> {row_limit_after}, "
            f"color_scheme {scheme_before!r} -> {scheme_after!r}"
        ),
        "mechanism": (
            "update_chart validates the caller's config into a fresh "
            "TableChartConfig and rebuilds form_data from it, so fields the "
            "caller omits take their pydantic defaults rather than the persisted "
            "values. `row_limit: int = Field(1000)` is non-optional, so the "
            "rebuild always writes 1000 over the stored limit. `color_scheme` "
            "defaults to None and chart_utils.add_color_scheme only writes the "
            "key when it is truthy, which is why the stored scheme survives at "
            "this revision — the data loss is field-dependent, not uniform. The "
            "additive `add_columns` path preserves the existing configuration; "
            "the `config` path does not."
        ),
        "code_paths": [
            "superset/mcp_service/chart/tool/update_chart.py::update_chart",
            "superset/mcp_service/chart/schemas.py::TableChartConfig",
            "superset/mcp_service/chart/chart_utils.py::add_color_scheme",
        ],
        "mcp_calls": calls,
        "persistence_path": "slices.params (Postgres metadata DB), read back via GET /api/v1/chart/<id>",
    }

    markdown = f"""# S1 — MCP chart update resets omitted `color_scheme` / `row_limit`

Status: **{"REPRODUCED" if reproduced else "NOT REPRODUCED"}** at the baseline
commit (recorded in `result.json`). Fields actually reset by the update:
**{", ".join(reset_fields) or "none"}**.

## User action

Through the Superset MCP service an assistant saves a table chart on the
synthetic `synthetic_orders` dataset with `row_limit={ROW_LIMIT}` and
`color_scheme='{COLOR_SCHEME}'`, then asks for one narrow change — sort by
`SUM(revenue)` — without restating the other settings.

## MCP service

No compose file at the baseline revision defines an MCP service, so it runs as
an explicit sidecar (`superset-mcp-light` in
`stack/docker-compose.ports.yml`), on the same image as the web service:

```
superset mcp run --host 0.0.0.0 --port 5008
```

Dependency check: `fastmcp 3.4.7` is already in the image. Authentication uses
`MCP_DEV_USERNAME` from `stack/superset_config_mcp.py`; the Superset checkout
is untouched.

## Steps

1. `generate_chart` (save_chart=true) → chart **{chart_id}**
2. `GET /api/v1/chart/{chart_id}` → `row_limit={row_limit_before}`,
   `color_scheme={scheme_before!r}`
3. `update_chart` with a config that only adds `sort_by`
4. `GET /api/v1/chart/{chart_id}` → `row_limit={row_limit_after}`,
   `color_scheme={scheme_after!r}`

## Expected vs observed

- Expected: {result["expected"]}
- Observed: {result["observed"]}

## Mechanism

{result["mechanism"]}

## Evidence

- `result.json` — MCP calls, before/after chart params and source revision.
- `transcript.json` — sanitized REST reads of the persisted chart.
- Persistence path: {result["persistence_path"]}.
"""

    directory = write_artifacts(SCENARIO_ID, result, transcript, markdown)
    print(json.dumps({"reproduced": reproduced, "fields": result["fields"], "artifacts": str(directory)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
