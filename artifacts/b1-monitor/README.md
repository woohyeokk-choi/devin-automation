# B1: what the scheduled browser monitor actually captured

Three scans of the isolated baseline stack (Compose project `repro43987`,
Superset `394bca55c792b7b3547e23f6e175a7cb0f0757e8`), each run from the monitor
**container image** (`Dockerfile.monitor`) on the Superset network, with
GitHub, Devin and Slack absent from that container and dispatch off:

```bash
docker compose -f stack/docker-compose.monitor.yml run --rm monitor \
    python -m portal.monitor --once
```

| File | Scan | Result |
| --- | --- | --- |
| `scan-1.json` | saved defect chart, `/explore/?slice_id=1` | 1 `qualified` finding (16 occurrences consolidated), 3 `ignored`, 0 `needs_attention` |
| `scan-2.json` | the same chart again, unchanged | the same qualified finding |
| `scan-control.json` | the small-value control chart, `/explore/?slice_id=2` | 0 findings above `ignored`; nothing to admit |

The qualified finding is the product's own message, at the product's own
severity:

```
warning  Formatter failed, falling back to raw value
         TypeError: Cannot convert a BigInt value to a number
```

with `visible_values` = `cold_archive | 1425300509404304697 | 4KiB |
warm_tier | 9007199254740993 | 8KiB` and one request summary,
`POST /api/v1/chart/data → 200`. No pageerror, no 500, no chart crash. The
monitor emitted nothing to the console itself and asserted no expected value;
those numbers are recorded as evidence, not used as the trigger.

`events.redacted.jsonl` is the event store those three runs wrote
(`monitor_scan` ×3, `browser_telemetry` ×2 — the control scan produced no
telemetry event). `provenance.json` is the measured stack identity the events
carry: running-code hash matching the pinned checkout, plus the B fixture
revision read back from the database.

Draining those five events through the incident engine, once per admission
mode:

| `PORTAL_TELEMETRY_ADMISSION` | Ingested | Incidents | Eligible |
| --- | --- | --- | --- |
| `disabled` | 0 | none | 0 |
| `dry_run` | 2 | 1, `blocked` — "telemetry admission is in dry-run mode" | 0 |
| `enabled` | 2 | 1, `eligible` | 1 |

Two defect scans, one incident, two evidence events: repeats consolidate rather
than proposing a second session, and the warning-free control contributes
nothing in any mode.

## The verifier, run against the unfixed baseline

`validator-baseline.json` is `portal.validator.case_b1` replayed against the
same stack. It reports `failed`: 4 of 11 checks do not hold — large values are
raw digits and the formatter warning is logged, on both the first and the
second load — while every setup and control check holds, including the
small-value control chart and `4KiB`/`8KiB` inside the affected chart. That is
the negative control for the grader: on a real candidate it has to change its
answer, and on this commit it does not pass.

## The frontend build the verifier depends on

A frontend defect cannot be graded against somebody else's bundle, so the
runner builds the candidate's own. `frontend-build.json` records that step
rehearsed on a worktree at the baseline commit, with the same image and
command `IsolatedStack._build_frontend` issues: 324 JavaScript files,
`sha256:76ac3b3151d3ca4c`. Two things this exposed, both fixed in the runner:
the web image has a Node runtime but no npm and no installed packages, so the
build belongs in a toolchain image over the checkout; and the webpack config
shells out to `zstd`, which a plain Node image does not carry.
