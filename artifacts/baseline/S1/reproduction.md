# S1 — MCP chart update resets omitted `color_scheme` / `row_limit`

Status: **REPRODUCED** at the baseline
commit (recorded in `result.json`). Fields actually reset by the update:
**row_limit**.

## User action

Through the Superset MCP service an assistant saves a table chart on the
synthetic `synthetic_orders` dataset with `row_limit=137` and
`color_scheme='googleCategory10c'`, then asks for one narrow change — sort by
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

1. `generate_chart` (save_chart=true) → chart **5**
2. `GET /api/v1/chart/5` → `row_limit=137`,
   `color_scheme='googleCategory10c'`
3. `update_chart` with a config that only adds `sort_by`
4. `GET /api/v1/chart/5` → `row_limit=1000`,
   `color_scheme='googleCategory10c'`

## Expected vs observed

- Expected: an update that only changes sort_by keeps row_limit 137 and color_scheme 'googleCategory10c'
- Observed: row_limit 137 -> 1000, color_scheme 'googleCategory10c' -> 'googleCategory10c'

## Mechanism

update_chart validates the caller's config into a fresh TableChartConfig and rebuilds form_data from it, so fields the caller omits take their pydantic defaults rather than the persisted values. `row_limit: int = Field(1000)` is non-optional, so the rebuild always writes 1000 over the stored limit. `color_scheme` defaults to None and chart_utils.add_color_scheme only writes the key when it is truthy, which is why the stored scheme survives at this revision — the data loss is field-dependent, not uniform. The additive `add_columns` path preserves the existing configuration; the `config` path does not.

## Evidence

- `result.json` — MCP calls, before/after chart params and source revision.
- `transcript.json` — sanitized REST reads of the persisted chart.
- Persistence path: slices.params (Postgres metadata DB), read back via GET /api/v1/chart/<id>.
