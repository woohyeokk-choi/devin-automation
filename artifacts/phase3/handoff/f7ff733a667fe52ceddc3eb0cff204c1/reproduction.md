# A discarded exploration link comes back pointing at new state

Scenario **S2** · family `discarded_form_data_key_is_reused` ·
fingerprint `f7ff733a667fe52ceddc3eb0cff204c1`

Saving a new exploration after discarding one reuses the discarded key, so an old link resolves again and shows a different exploration's state.

## Environment

| | |
|---|---|
| Target repository | `woohyeokk-choi/superset` |
| Baseline SHA | `394bca55c792b7b3547e23f6e175a7cb0f0757e8` (provenance: running-code-hash+mount-match) |
| Fixture revision | `sha256:67ff039835f9890c` |
| Failed user actions | 7 |
| Evidence events | 7 |
| First seen | 2026-09-19T16:06:38.264+00:00 |
| Last seen | 2026-09-19T16:11:13.407+00:00 |

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

1. Sign in to the portal with the demo credentials and stay in the
   same workspace for the whole scenario. The defect is about one workspace's
   own links, so pressing "Start another exploration" (which rotates the
   workspace) is a negative control, not a reproduction.
2. On the dashboard, save an exploration (any dimension/sort/row limit) and
   copy the link it produces.
3. Open that link once; it shows the exploration you saved.
4. Discard the exploration from the dashboard list.
5. Open the link again: it is gone (HTTP 404). This is the correct behaviour
   and the last correct step.
6. Save a *different* exploration in the same workspace.
7. Open the original link again. Expected: still gone. Observed at baseline:
   it resolves and shows the new exploration's state.

## Contract assertions recorded

- `discarded_exploration_link_stays_gone`
- `new_exploration_does_not_reuse_a_discarded_key`

### Violations

- `new_exploration_does_not_reuse_a_discarded_key`: expected `"a key that is not a previously discarded link"`, observed `"the key of a discarded exploration"`
- `discarded_exploration_link_stays_gone`: expected `"HTTP 404 for a discarded exploration"`, observed `"HTTP 200"`

## Evidence

`events.redacted.jsonl` holds every event behind this incident, ordered by
trace and request step, sanitized by the same allowlist that writes the log.
No credentials, cookies, tokens or raw bodies are included.
