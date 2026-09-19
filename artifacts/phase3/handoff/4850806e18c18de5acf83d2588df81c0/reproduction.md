# An MCP chart update resets fields the caller omitted

Scenario **S1** · family `omitted_row_limit_is_reset` ·
fingerprint `4850806e18c18de5acf83d2588df81c0`

Updating only the sort of a table chart through MCP resets the omitted row limit to the default instead of leaving it alone.

## Environment

| | |
|---|---|
| Target repository | `woohyeokk-choi/superset` |
| Baseline SHA | `394bca55c792b7b3547e23f6e175a7cb0f0757e8` (provenance: running-code-hash+mount-match) |
| Fixture revision | `sha256:67ff039835f9890c` |
| Failed user actions | 2 |
| Evidence events | 2 |
| First seen | 2026-09-19T16:06:39.607+00:00 |
| Last seen | 2026-09-19T16:12:43.434+00:00 |

## Rebuild the environment

```bash
# 1. The product, at the SHA this incident was observed on. Use a separate
#    checkout: the light stack bind-mounts this directory, so verifying another
#    commit means pointing SUPERSET_DIR at that commit, not rebuilding over it.
git clone https://github.com/woohyeokk-choi/superset.git superset
git -C superset checkout 394bca55c792b7b3547e23f6e175a7cb0f0757e8

# 2. The automation repo (portal, scenarios, fixtures)
git clone https://github.com/woohyeokk-choi/devin-automation.git
cd devin-automation
cp stack/.env.example stack/.env        # set SUPERSET_DIR/AUTOMATION_DIR
set -a; . stack/.env; set +a

# 3. Superset + MCP sidecar, loopback only
(cd "$SUPERSET_DIR" && docker compose -f docker-compose-light.yml \
   -f "$AUTOMATION_DIR/stack/docker-compose.ports.yml" \
   up -d superset-light superset-mcp-light)

# 4. Deterministic synthetic fixture (600 rows, revision sha256:67ff039835f9890c)
python3 scripts/seed_synthetic.py

# 5. Measured provenance for the running containers, then the portal
python3 scripts/capture_provenance.py
docker compose -f stack/docker-compose.portal.yml up -d --build
curl -s http://127.0.0.1:8090/healthz

# 6. Replay every scenario headlessly instead of clicking (same assertions)
python3 scripts/export_examples.py --out artifacts/<run-id>/examples
```

## Reproduce the user action

1. Sign in to the portal with the demo credentials.
2. Reset the fixture (operator console → "Reset demo fixture") so the chart
   starts at row limit 137, highest revenue first, googleCategory10c.
3. Open "Chart settings" and confirm that starting state is read back.
4. Change only the sort direction. Do not touch the row limit; the portal
   omits it from the MCP update exactly as the customer's request does.
5. Read the settings back. Expected: row limit still 137. Observed at
   baseline: it has been reset to 1000. The colour scheme is preserved,
   which is the control that keeps this narrow.

## Contract assertions recorded

- `row_limit_survives_an_unrelated_change`

### Violations

- `row_limit_survives_an_unrelated_change`: expected `137`, observed `1000`

## Evidence

`events.redacted.jsonl` holds every event behind this incident, ordered by
trace and request step, sanitized by the same allowlist that writes the log.
No credentials, cookies, tokens or raw bodies are included.
