# S2 — deleted explore form-data key is reused

Status: **REPRODUCED** at the
baseline commit (`` recorded in
`result.json`).

## User action

In the portal a user explores the synthetic `synthetic_orders` dataset in one
browser tab, discards that exploration state, then starts a new exploration in
the same tab.

## Steps (plain REST, one authenticated cookie session, non-empty `tab_id=991177`)

1. `POST /api/v1/explore/form_data?tab_id=991177` → key **K1** = `pkfdb3k84e7Sia46-DqETw`
2. `GET /api/v1/explore/form_data/{K1}` → 200, exploration A
3. `DELETE /api/v1/explore/form_data/{K1}` → 200
4. `GET /api/v1/explore/form_data/{K1}` → 404 (absence confirmed)
5. `POST /api/v1/explore/form_data?tab_id=991177` → key **K2** = `pkfdb3k84e7Sia46-DqETw`
6. `GET /api/v1/explore/form_data/{K1}` → resurrected = `True`

## Expected vs observed

- Expected: K2 is a fresh key; K1 stays deleted and keeps returning 404 regardless of later explorations in the same tab
- Observed: K2 == K1 (True); after the second exploration the deleted key resolves again (resurrected=True) and now serves the new exploration's form data

## Mechanism (hypothesis 1 — missing `tab_id` on DELETE, REST-only)

POST /api/v1/explore/form_data reads tab_id from the query string and stores a contextual mapping cache_key(session_id, tab_id, datasource_id, chart_id, datasource_type) -> key. DELETE /api/v1/explore/form_data/<key> builds CommandParameters(key=key) without tab_id, so the delete command deletes the contextual mapping for tab_id=None and leaves the real mapping in place. The next create finds that stale mapping and hands out the deleted key again.

The MCP session-id override (`MCPCreateFormDataCommand._get_session_id`) is a
**separate** hypothesis: it is not involved here, since this reproduction never
touches the MCP service and uses a single cookie session throughout.

## Evidence

- `transcript.json` — sanitized requests/responses (no cookies, no CSRF tokens).
- `result.json` — machine-readable result, keys, statuses and source revision.
- Cache backend: SupersetMetastoreCache (key_value table, Postgres).
