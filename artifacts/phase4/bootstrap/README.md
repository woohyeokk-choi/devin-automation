# Fresh-host bootstrap, isolated namespace

A bootstrap run in a Compose namespace of its own, so nothing here depends on
the already-seeded host stack. No existing environment or artifact was deleted
to make it pass.

**Correction (Phase 5).** The original wording called this a run "from clean
checkouts". The measurement in `provenance.json` says otherwise:
`automation.checkout_dirty = true` at `4882bf7`, because the automation
checkout it ran from carried uncommitted Phase 4 work. The Superset checkout
was clean at the baseline commit, and the Compose namespace was genuinely
isolated, so the S2 reproduction recorded here stands. What it does not show
is a bootstrap of *published* automation code. `provenance.json` is left
exactly as it was measured; the run that uses only committed code is
`artifacts/phase5/bootstrap/`.

| Fact | Value |
| --- | --- |
| Automation checkout | `/home/ubuntu/bootstrap-check/devin-automation` (fresh clone) |
| Superset checkout | `/home/ubuntu/bootstrap-check/superset` (fresh clone) |
| Compose project | `bootstrapcheck` (network `bootstrapcheck_default`, image `bootstrapcheck-superset-light`) |
| Published ports | Superset 8188, MCP 5108, portal 8190 — all loopback |
| Superset SHA | `394bca55c792b7b3547e23f6e175a7cb0f0757e8` |
| Automation SHA at the time of the run | `4882bf7b35dcac7c57907c96588b80461cb53cff`, tree **dirty** (see correction above) |
| Seeded rows | 600 |
| Fixture revision | `sha256:67ff039835f989c0` |
| Portal health | `{"status":"ok","environment_kind":"baseline-light"}` |
| S2 | Reproduced — see `s2_result.json` |

Observed while running it:

- The seed derives its database container from `SUPERSET_COMPOSE_PROJECT` /
  `COMPOSE_PROJECT_NAME`, checks the Compose project identity and the Superset
  port publication before writing, and refuses a namespace it was not pointed
  at. Host scripts use loopback URLs; only services inside Compose use
  `superset-light` / `superset-mcp-light` names.

Known caveat at the time, explained in Phase 5:

- The MCP container in this namespace reported Docker health `unhealthy` even
  though the S2 scenario and the portal health check both succeeded. The cause
  was the inherited image healthcheck (`docker/docker-healthcheck.sh`) curling
  the *web* application's `/health` on `SUPERSET_PORT`; the sidecar serves MCP
  on 5008 and has no such route. The overlay now probes the MCP listener
  itself — see `artifacts/phase5/bootstrap/`.

Files: `provenance.json` (measured container/source/fixture identity),
`s2_result.json` (the reproduction as the scenario harness wrote it).
