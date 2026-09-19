# Baseline reproduction artifacts

Everything under `artifacts/baseline/` was produced by running the scenarios in
`scenarios/` against the Superset fork at
`394bca55c792b7b3547e23f6e175a7cb0f0757e8` (tag `baseline-394bca5`). Each
`result.json` carries the source revision that actually executed.

| Path | Contents |
| --- | --- |
| `baseline/manifest.json` | Fixture/config manifest: stack, image, container Python, effective `SUPERSET_CONFIG_PATH`, every cache backend, synthetic fixture, and the exact commands |
| `baseline/S2/` | Primary defect — deleted explore form-data key is reused |
| `baseline/S1/` | Secondary defect — MCP chart update resets an omitted `row_limit` |
| `baseline/N1/` | Control — a legitimate permission denial stays 403 |

Each scenario directory holds `reproduction.md` (narrative: user action, steps,
expected vs observed, mechanism), `result.json` (machine-readable result the
controller will consume) and `transcript.json` (sanitized requests/responses —
cookies, CSRF tokens and authorization headers are replaced with `<redacted>`).

No screenshots are included: all three scenarios are driven through the REST
and MCP interfaces, so the transcripts are the primary evidence.

## Reproducing from scratch

```bash
# 1. stack (no frontend build; the REST/MCP reproductions do not need it)
cd /home/ubuntu/repos/superset
docker compose -f docker-compose-light.yml \
  -f /home/ubuntu/repos/devin-automation/stack/docker-compose.ports.yml \
  up -d superset-light superset-mcp-light

# 2. fixtures
cd /home/ubuntu/repos/devin-automation
python3 scripts/seed_synthetic.py --reset

# 3. scenarios (rewrite the artifacts in place)
python3 scenarios/s2_form_data_key_reuse.py
python3 scenarios/s1_mcp_update_resets_fields.py
python3 scenarios/n1_permission_denied_control.py
python3 scripts/capture_manifest.py
```

## Verifying a PR, not the baseline

`docker-compose-light.yml` bind-mounts `./superset` and `./docker` into the
container, so rebuilding the image while the baseline source is mounted still
runs baseline code. Verification of a repair PR must therefore use an isolated
clone checked out at the exact PR SHA (its own Compose project name) or run with
those mounts removed. The `revision` block in every `result.json` records which
source tree was executed, so a verification run that silently used baseline
source is detectable after the fact.
