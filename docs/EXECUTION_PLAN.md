# Devin Runtime Repair — Execution Plan

Single living plan. Updated at the end of every phase: decisions, phase status,
commands actually run with their results, and open blockers.

Last updated: Phase 2 (2026-09-19).

---

## 1. Goal

A synthetic-data analytics portal backed by a real Apache Superset fork. When a
real user action fails or returns incorrect state, server-side structured logs
produce a deduplicated incident. A controller creates or reuses an issue on the
fork and calls the Devin API. That API-created session reconstructs the
environment, reproduces the problem, fixes product code and opens a PR.
Independent verification replays the same scenario against the PR commit. Failed
behavioral checks go back to the same session, with at most two repair
follow-ups. An operator console links logs → incident → reproduction → session →
PR → verification.

Passing criterion: `verified_in_preview`. Nothing is deployed.

## 2. Repositories and branches

| Role | Repository | State found in Phase 0 |
| --- | --- | --- |
| Automation | `woohyeokk-choi/devin-automation` | Empty — no commits, no refs on the remote; default branch `main` unborn. |
| Target | `woohyeokk-choi/superset` | Single branch `master`, clean tree, HEAD = `394bca55c792b7b3547e23f6e175a7cb0f0757e8` ("fix(mcp): preserve dashboard filter values for restricted users (#44107)", 2026-09-17). |

The fork's `master` is **exactly** the research baseline commit, so no rebase,
reset or divergence handling is needed and nothing existing can be undone.

Branch decisions:

- **Superset fork**
  - `master` stays untouched and is the immutable baseline reference.
  - Create `runtime-repair/baseline` from `master` (identical commit). This is
    the isolated integration branch: portal-support commits (synthetic data,
    structured-logging config, scenario fixtures) land here, and **every
    API-created repair PR targets this branch**, never `master` and never
    `apache/superset`.
  - Tag `baseline-394bca5` on the baseline commit so verification can always
    diff a PR head against a fixed point.
  - Repair sessions work on their own `devin/<incident-id>-*` branches cut from
    `runtime-repair/baseline`.
- **Automation repo**
  - Initialize `main` with the minimal planning files (this plan plus
    `.gitignore` and a short `README.md` pointing at it). A PR is impossible
    while the repo has no default branch, so the first commit goes directly to
    `main`; all later work uses feature branches and PRs into `main`.

Nothing in either repository is reset, force-pushed, merged or deleted.

## 3. Phase 0 findings — facts vs unknowns

### Facts (verified this session)

- Machine: Ubuntu 22.04, 8 vCPU, 31 GiB RAM, 113 GiB free on `/`, Docker
  29.7.2 + Compose v5.4.0, Python 3.10.12.
- `docker pull redis:7` succeeded, so image pulls from Docker Hub work.
- Superset ships four compose files: `docker-compose.yml` (dev, live reload,
  nginx + redis + postgres + superset + node + worker + beat + websocket),
  `docker-compose-light.yml` (postgres + superset + init + node, plus a
  `pytest-runner` profile), `docker-compose-non-dev.yml`, and
  `docker-compose-image-tag.yml`. Build target defaults to `dev`.
- Structured logging can be injected **without touching product code**:
  `superset/config.py` exposes `EVENT_LOGGER = DBEventLogger()` (line 93) and
  `LOGGING_CONFIGURATOR = DefaultLoggingConfigurator()` (line 1676), both
  overridable from `docker/pythonpath_dev/superset_config.py`, which is already
  mounted into the containers. `AbstractEventLogger` lives in
  `superset/utils/log.py`.
- Candidate **S1** (MCP chart update drops omitted `color_scheme` / `row_limit`)
  maps to `superset/mcp_service/chart/tool/update_chart.py` plus the per-viz
  plugins in `superset/mcp_service/chart/plugins/`. Most plugins call
  `config.model_dump()` without `exclude_unset=True` (only `gauge.py` and the
  validation pipeline use `exclude_unset`), which is a plausible mechanism for
  omitted fields being reset to schema defaults.
- Candidate **S2** (deleted contextual form-data keys are reused) maps to
  `superset/commands/explore/form_data/{create,delete}.py` and
  `superset/mcp_service/commands/create_form_data.py`. `create` stores a
  `contextual_key → key` mapping derived from `cache_key(session_id, tab_id,
  datasource_id, chart_id, datasource_type)` and reuses it on the next create;
  `delete` computes the contextual key from `session.get("_id")` while the MCP
  subclass `MCPCreateFormDataCommand` overrides the session id with the user id.
  A mismatch or a `tab_id`-less delete leaves the contextual mapping pointing at
  a deleted state key — the observable symptom is a reused/stale key.
- S2 is reachable over plain HTTP: `superset/explore/form_data/api.py` exposes
  `POST /api/v1/explore/form_data`, `PUT|GET|DELETE
  /api/v1/explore/form_data/<key>`.
- S1 is **not** reachable from the standard compose stack: there is no `mcp`
  service or profile in any compose file at this commit (the MCP README's
  `--profile mcp` instructions do not match the code here). The MCP server must
  be started as a separate process via `superset mcp run --host <h> --port 5008`
  (`superset/cli/mcp.py`).
  *Correction (Phase 1):* the image choice is not constrained to `dev`. The
  `superset` target installs `.[postgres,mysql,fastmcp]` (Dockerfile line 315),
  so the batteries-included image ships the MCP dependency too; `dev` gets it
  transitively from `requirements/development.txt` (`fastmcp==3.4.7`). What S1
  needs is a separate MCP **service**, not a particular build target.
- Superset AGENTS.md mandates `pre-commit run` on changed files; the repo has a
  `.pre-commit-config.yaml` (mypy, ruff, black/oxfmt, eslint).

### Unknowns / not yet verified

- Whether the Superset dev image builds end to end on this box and how long a
  cold build plus `superset init` takes.
- Whether S1 and S2 actually reproduce at this commit through a real user action
  (they remain *candidates* until an application-level reproduction succeeds).
- Whether the MCP service can be driven from the portal cheaply enough to be a
  "real user action" rather than an agent-only path.
- Devin API access: no `DEVIN_API_KEY` is present, and whether API-created
  sessions can clone/push to the fork is unverified.
- GitHub API credentials for the controller (issue create/reuse, PR read) are
  not present; git CLI access works only through the session's git proxy, which
  the controller cannot rely on.

## 4. Recommended approach (and where it differs from the brief)

The brief's architecture is kept: a small FastAPI service, HTTPX for outbound
calls, SQLite for state, and Superset running as separate services.

```
devin-automation/
  app/
    main.py            FastAPI app: portal API, log intake, console, webhooks
    portal/            synthetic-data analytics portal (thin UI over Superset)
    logging/           structured log tail/intake + normalization
    incidents/         fingerprinting, dedup, state machine
    controller/        GitHub issue create-or-reuse + Devin API session create
    verification/      scenario replay against a PR commit, bounded feedback
    console/           operator console (incident → session → PR → verification)
    db.py              SQLite (WAL) + schema/migrations
  scenarios/           declarative scenario definitions (S1, S2, N1)
  docs/EXECUTION_PLAN.md
```

- **Transport**: FastAPI + HTTPX + SQLite, single process, no Celery. Background
  work runs on FastAPI background tasks / a small poller loop.
- **Superset stays separate**: its own compose project on its own ports; the
  automation service never imports Superset code and only talks to it over HTTP.
- **Incident identity**: fingerprint = `(scenario_id, error_class, normalized
  code location, observed-vs-expected shape)`; repeated occurrences increment a
  counter on one incident instead of creating new ones.
- **Trigger safety**: `AUTO_REPAIR_ENABLED=false` by default. The controller
  computes and records the action it *would* take until the flag is flipped.
- **Verification**: a replay runner executes the same scenario script against a
  stack built from the PR head commit, and writes `verified_in_preview` or a
  structured failure report. At most two repair follow-ups are sent back to the
  same session; the third failure parks the incident as `needs_human`.

Recommended changes to the proposed approach, with evidence:

1. **Make S2 the primary scenario and S1 the secondary one.** Evidence: S2 is
   driven entirely by the public `/api/v1/explore/form_data` REST endpoints that
   a portal user action already hits, whereas S1 requires a separately launched
   MCP server that has no compose service at this commit and whose deps are
   dev-only. S1 stays in scope, but behind an explicit MCP sidecar we add to the
   compose project.
2. **Use `docker-compose-light.yml` as the default stack** (postgres + superset
   + init + node, and a `pytest-runner` profile) instead of the full dev compose,
   and keep the heavier `docker-compose.yml` only if a scenario needs celery or
   websockets. Evidence: the light file omits nginx, redis, worker, beat and
   websocket services, which cuts cold-build and boot cost on an 8 vCPU box, and
   it already exposes a test-runner profile useful for verification.
3. **Emit structured logs through a config-level `EVENT_LOGGER` /
   `LOGGING_CONFIGURATOR` override in `docker/pythonpath_dev/`, not by editing
   Superset code.** Evidence: both are documented override points in
   `superset/config.py`, so the product diff stays limited to the actual defect
   fix — which keeps repair PRs clean and keeps verification honest.
4. **Verify against the PR commit by rebuilding the light stack at that SHA**
   rather than hot-patching a running container, so `verified_in_preview` means
   the branch itself is good.
5. **Pin the baseline with a tag and a dedicated integration branch**
   (`baseline-394bca5`, `runtime-repair/baseline`) so repeated repair rounds
   never drift and `master` is never written to.

## 5. Phase plan

| Phase | Scope | Exit criteria |
| --- | --- | --- |
| 0 | Repo/branch state, feasibility assessment, this plan. | Plan committed; branch strategy agreed; blockers listed. **Complete.** |
| 1 | Environment and scenarios: build the light stack, `superset init`, load synthetic data, script S1/S2/N1 as executable scenario definitions, confirm or refute each candidate by real reproduction. | Stack boots; S1/S2 reproduce (or are replaced); N1 returns 403 with no repair path. |
| 2 | Portal and logging: FastAPI portal over Superset, structured JSON logs from Superset config override + portal-side action logs, log intake into SQLite. | A failing user action produces a structured log record end to end. |
| 3 | Incident console: fingerprinting, dedup, state machine, operator console linking logs → incident → reproduction. | Repeated failures collapse into one incident, visible in the console. |
| 4 | GitHub + Devin integration: issue create-or-reuse on the fork, Devin API session creation with a reproduction-bearing prompt, session/PR tracking. Still `AUTO_REPAIR_ENABLED=false` — dry-run recorded, no paid sessions. | Controller produces the exact issue body and API payload it would send, stored and shown in the console. |
| 5 | Independent verification and bounded feedback: replay the scenario against the PR commit, record `verified_in_preview` / failure, return failures to the same session with the two-follow-up cap. | Verification runs against a simulated/PR commit and the cap is enforced. |
| 6 | Live API-created repairs: flip the flag for a controlled run; real sessions, real PRs, real verification. | **Two independent live remediations reach `verified_in_preview`** (the pilot target). One successful incident is an intermediate milestone, not the exit criterion. |
| 7 | README, evidence and Loom. | Evidence bundle and walkthrough complete. |

## 6. Phase status

- **Phase 0 — complete.** Repos inspected, baseline confirmed, feasibility
  assessed, plan written and reviewed. Decisions approved: S2 primary / S1
  secondary, `docker-compose-light.yml` first, `baseline-394bca5` tag and
  `runtime-repair/baseline` branch created at the baseline commit, `master`
  untouched.
- **Phase 1 — complete.** Light stack built and running, synthetic fixtures
  seeded, S2 and S1 reproduced against the baseline commit and N1 confirmed as
  a clean denial. Artifacts in `artifacts/baseline/`. See §7b.
- **Phase 2 — complete.** Portal, structured event log, redaction and operator
  viewer built and exercised through a browser against the running baseline.
  See §7c. Three review findings were closed afterwards — see §7d.
- **Phase 3 — complete.** Incident model, protected console and handoff export
  built on the tested portal slice. See §7e.
- **Phase 4 — complete, offline.** GitHub/Devin v3 clients, the repair
  controller with a database-enforced single-flight claim, durable creation
  intents, budget policy and a background poller. No issue, session or message
  was ever sent: `AUTO_REPAIR_ENABLED=false` throughout. See §7f.
- **Phase 5 — complete, offline.** Dispatch moved off the request path into
  the durable worker, the independent verifier (pull-request validation,
  change scope, isolated candidate stack, measured provenance, bounded
  same-session feedback), and the real validator run against an isolated
  baseline as a negative control. No repaired code exists to pass, and none
  was fabricated. See §7g.
- **Phase 6 — complete, live.** Two genuine pilots (S2 then S1) each went from
  a real portal failure to a real fork issue, exactly one API-created session
  and an independently verified pull-request head. Both stand at
  `verified_in_preview`; neither is merged or deployed. See §7h.
- **Phase 7 — complete.** README reconciled with the published branch, a
  credential-free `--network none` simulation verified from a clean clone,
  `docs/results.md`, `docs/demo-script.md` and `docs/submission.md` written,
  bounded browser QA of the live console with dispatch off, and the two
  console defects that QA found fixed. See §7j.

## 7. Commands run and results (Phase 0)

| Command | Result |
| --- | --- |
| `git clone .../devin-automation` | Empty repository; `main` unborn; `git ls-remote` returns no refs. |
| `git clone .../superset` | Full clone (not shallow); only `master`; clean working tree. |
| `git log -1` (superset) | `394bca55c792b7b3547e23f6e175a7cb0f0757e8` — equals the research baseline. |
| `docker --version` / `docker compose version` | 29.7.2 / v5.4.0. |
| `docker pull redis:7` | Success — registry reachable. |
| `df -h /`, `free -g`, `nproc` | 113 GiB free, 31 GiB RAM, 8 cores. |
| Code inspection of `mcp_service/chart`, `commands/explore/form_data`, `explore/form_data/api.py`, `config.py`, `cli/mcp.py`, compose files | See §3 Facts. |

No Superset build, no Superset code change, no Devin API call, and no paid
session were made in this phase.

## 7b. Phase 1 results

### Stack

| Fact | Value |
| --- | --- |
| Compose | `docker-compose-light.yml` + `stack/docker-compose.ports.yml` (publishes 8088; adds the MCP sidecar) |
| Cold image build | `docker compose -f docker-compose-light.yml build superset-light` — **1m 08s** (log: `/home/ubuntu/artifacts/build_light.log`) |
| Stack start | `up -d superset-light` — 34s to healthy (`/health` → 200) |
| Frontend build | **not started** — the REST and MCP reproductions do not need it |
| Container Python | 3.11.14 (web and MCP containers) |
| `SUPERSET_CONFIG_PATH` | `/app/docker/pythonpath_dev/superset_config_docker_light.py` |
| `CACHE_CONFIG` / `DATA_CACHE_CONFIG` | `SimpleCache`, TTL 300, prefix `superset_light_` (the light config overrides the Redis default; the light stack runs no Redis) |
| `EXPLORE_FORM_DATA_CACHE_CONFIG` | `SupersetMetastoreCache`, TTL 604800, `JsonKeyValueCodec` — **Postgres `key_value` table**, inherited from `superset/config.py`, not overridden by the light config |
| `FILTER_STATE_CACHE_CONFIG` | `SupersetMetastoreCache`, TTL 7776000, `JsonKeyValueCodec` — same metastore backend |
| `RESULTS_BACKEND` | `FileSystemCache` at `/app/superset_home/sqllab` |

Because the two temporary-cache configs are metastore-backed, S2's state is
durable in Postgres and survives container restarts — the reproduction is not
an artifact of an in-process cache.

### S2 — REPRODUCED (primary)

One cookie session, non-empty `tab_id=991177`, plain REST:
create A → `K1` → `GET K1` 200 → `DELETE K1` 200 → `GET K1` **404** →
create B → `K2` **== K1** → `GET K1` returns exploration **B**.

The deleted key is handed out again and resurrects. Mechanism (hypothesis 1,
confirmed, REST-only): `POST /api/v1/explore/form_data` reads `tab_id` from the
query string and stores a contextual mapping
`cache_key(session_id, tab_id, datasource_id, chart_id, datasource_type) → key`,
but `ExploreFormDataRestApi.delete` builds `CommandParameters(key=key)` with no
`tab_id`, so `DeleteFormDataCommand` deletes the mapping for `tab_id=None` and
leaves the real one behind. Direct evidence: after the delete, one
`superset_metastore_cache` row (the stale mapping) remains in `key_value`.

The MCP session-id override (`MCPCreateFormDataCommand._get_session_id`) is a
separate hypothesis and plays no part here — the MCP service is not involved.

### S1 — REPRODUCED, narrower than the candidate (secondary)

MCP `generate_chart` (saved, `row_limit=137`, `color_scheme='googleCategory10c'`)
followed by `update_chart` with a config that only adds `sort_by`:
**`row_limit` 137 → 1000**, `color_scheme` **preserved**.

`update_chart` re-validates the caller's config into a fresh `TableChartConfig`
and rebuilds `form_data`, so omitted fields take pydantic defaults.
`row_limit: int = Field(1000)` is non-optional and always overwrites the stored
value; `color_scheme` defaults to `None` and `add_color_scheme` only writes the
key when truthy, so the stored scheme survives. The loss is therefore
field-dependent — the incident should be framed as "omitted `row_limit` is
reset", not "omitted settings are reset".

MCP service facts: no compose file at this revision defines one, so it runs as
the `superset-mcp-light` sidecar on the same image
(`superset mcp run --host 0.0.0.0 --port 5008`); `fastmcp 3.4.7` is already in
the image; auth via `MCP_DEV_USERNAME` in `stack/superset_config_mcp.py` (kept
in this repo so the Superset checkout stays byte-identical); the tool-search
transform is on, so tools are invoked through the `call_tool` proxy with the
arguments wrapped in `request`.

### N1 — PASS (control)

`restricted_analyst` (role `Gamma`) performs the same explore write: **403**;
the same user's chart listing returns 200. Authorization and CSRF stayed
enabled throughout — the only change was adding a low-privilege user. The
event must be classified `expected_denial`: no incident, no issue, no repair.

### Artifacts

`artifacts/README.md` (retrieval and re-run instructions),
`artifacts/baseline/manifest.json` (fixture/config manifest), and
`artifacts/baseline/{S1,S2,N1}/{reproduction.md,result.json,transcript.json}`.
Transcripts are sanitized: cookies, CSRF tokens and authorization headers are
`<redacted>`. No screenshots — all three scenarios are API-driven.

### PR-verification constraint (recorded now, enforced in Phase 5)

`docker-compose-light.yml` bind-mounts `./superset` and `./docker`, so an image
rebuilt with baseline source mounted still runs baseline code. Verification
must use an isolated clone at the exact PR SHA under its own Compose project
name, or drop those mounts. Every `result.json` records the executed source SHA,
the fixture revision and whether the tree was dirty.

## 7c. Phase 2 results — portal and logging

### What was built

FastAPI + server-rendered Jinja, no frontend framework, no telemetry stack; the
only new service is the portal itself. It drives the same REST/MCP clients the
Phase 1 scenarios use (moved to `clients/`), so every page renders real
Superset state.

| Module | Role |
| --- | --- |
| `portal/config.py` | Environment-driven settings; no host paths are baked in. |
| `portal/redaction.py` | Allowlist `pick()` + recursive `scrub()` over values *and* free-form text. |
| `portal/events.py` | SQLite store + identical safe JSON to stdout + JSONL export. |
| `portal/provenance.py` | Loads the measured container/source/fixture revision; `unmeasured` when absent. |
| `portal/tracing.py` | `trace_id` per user action, `request_id` + `step_index` per upstream call, contract assertions. |
| `portal/upstream.py` | Superset REST and MCP gateways; outcome classification. |
| `portal/domain.py` | Customer actions: explorations, revenue table, chart settings, sort change. |
| `portal/app.py` + `portal/templates/` | Customer pages and the protected operator viewer. |

### Scope correction carried from Phase 1

The native Superset frontend is not built at this revision (no compiled assets
are served by the light stack), so the portal renders the real data and real
chart settings itself rather than embedding Superset's UI. This is stated
plainly rather than worked around; a frontend build is deferred until something
actually requires it.

### Customer-visible behaviour

- **S2** — save an exploration, open its link, discard it, save a *different*
  exploration in the same workspace, then re-open the first link: it comes back
  alive showing the second exploration's state. That is the customer-facing
  stale link. No HTTP failure is manufactured; the trace records
  `assertion_failed` on the fresh-key and A/B-content contracts.
  "Start over in a fresh workspace" rotates the tab id, so the discarded link
  stays gone — that path is the negative control, not the reproduction.
- **S1** — `/settings` reads the chart back after a sort change: the requested
  sort took effect (`ok`), `color_scheme` is preserved (`ok`, control), and the
  omitted `row_limit` is reset 137 → 1000 (`assertion_failed`). An explicit
  `row_limit` change is a normal control and stays `ok`.
- **N1** — the restricted profile gets a "Not available for your role" page;
  the upstream 403s and the portal's own chart-edit policy are recorded as
  `expected_denial`. Nothing escalates.

A denial is never confused with a failure: unreachable upstream, a missing
fixture or failed login is `blocked`, and `tests/test_outcomes.py` asserts that
a setup failure cannot masquerade as a verdict.

### Baseline defects vs harness health

At this baseline a correct replay *must* produce failed assertions, so the two
kinds are separated everywhere rather than summed into one "tests failed"
number:

- Assertions carry `subject` (`product_contract` / `harness`) and
  `known_baseline_defect`. The S2 key-reuse, S2 stale-link and S1 row-limit
  contracts are the known baseline defects.
- `/ops` shows separate **Baseline defects**, **Harness failures**, **Expected
  denials** and **Blocked** columns; trace detail tags a known defect with
  "known baseline defect — expected here".
- `artifacts/examples/{S2,S1,N1}/verdict.json` is the machine-readable form:
  `baseline_defect_reproduced`, `harness_failures`, `blocked_steps`,
  `expected_denials`, `not_applicable`, `harness_healthy`.

Superset stays untouched: these defects are the target of the Phase 6 repair
sessions, not something this builder session fixes.

### Deterministic fixture reset

The first browser run had to restore the row limit by hand because an earlier
run had left it at 1000. `POST /ops/fixtures/reset` (button on `/ops`, also the
first step of `scripts/export_examples.py`) now forgets the portal's
exploration records and restores **only** the demo chart to row limit 137,
highest-revenue-first and `googleCategory10c`, then asserts (as a `harness`
check) that it reached that state. A replay therefore always starts from the
same documented "before" values, and a reproduction cannot depend on leftovers.

### Not applicable

Submitting the chart-sort form as the restricted profile cannot be exercised:
the settings page is denied to that role, so the form is never rendered. It is
recorded as `not_applicable` with that reason in `N1/verdict.json`. The form is
not exposed and authorization is not weakened to manufacture coverage.

### Logging, storage and export

| Fact | Value |
| --- | --- |
| SQLite | `/data/events.sqlite` inside the portal container, on the `$PORTAL_DATA_DIR` host bind mount (a named volume until Phase 5's coordinator wiring; the Phase 2 rows below were recorded on `stack_portal_data`) |
| stdout | identical sanitized JSON records (`docker logs stack-portal-1`) |
| Export | `GET /ops/export.jsonl` (HTTP Basic) |
| Examples | `artifacts/examples/{S2,S1,N1}/events.redacted.jsonl` + `verdict.json` |
| Replay command | `python3 scripts/export_examples.py` (reset → S2 → S1 → N1 → export) |
| Screenshots | `artifacts/examples/screenshots/` |

### Verified in Phase 2

| Check | Command | Result |
| --- | --- | --- |
| Unit + canary tests | `python3 -m pytest tests -q` | 8 passed — canaries in headers, nested input, nested output and exception text appear in none of stdout / SQLite bytes / JSONL export; unreachable upstream classifies as `blocked`; an `assertion_failed` keeps `http_status` unset |
| Fresh reset + replay | `python3 scripts/export_examples.py` | S2 reproduced both defects (31 events), S1 reproduced 137 → 1000 (25 events), N1 2 expected denials (20 events); `harness_healthy: true` for all three, no manual fix-up |
| Browser click-through after reset | recorded run, screenshots in `artifacts/examples/screenshots/` | reset restored 137/highest-first/`googleCategory10c`; S1 `trace_72d09f9adc00`, S2 `trace_3357e53dd60b` + `trace_6800108c60e5`, N1 `trace_e89f313b7b26` + `trace_98aacb9a1447`; baseline defects flagged as expected, harness failures 0 |
| Persistence across container replacement | `docker compose -f stack/docker-compose.portal.yml rm -sf portal && ... up -d` | 598 events before removal, 598 after recreation; `stack_portal_data` → `/data/events.sqlite` |
| Internal MCP reachability | `docker exec stack-portal-1 python -c "urllib.request.urlopen('http://superset-mcp-light:5008/mcp')"` | reachable by service name (HTTP 405 for GET), no published port needed |
| Network safety | `ss -ltn`, `curl http://<host-ip>:{8088,5008,8090}` | all three listen on `127.0.0.1` only; every host-IP request fails |
| Provenance | `python3 scripts/capture_provenance.py` | `running-code-hash+mount-match`, container `superset-light`, checkout `394bca55`, clean, fixture `sha256:67ff039835f9890c` |

### Not verified in Phase 2

- No incident engine, fingerprinting or dedup (Phase 3).
- No issue creation, Devin API call, repair session or Slack message.
- No product fix; the Superset checkout is untouched and still at
  `baseline-394bca5`.
- No authenticated public preview URL — the stack is loopback-only by design,
  and the admin MCP endpoint is not exposed merely to publish one.
- No multi-user or load behaviour; the portal is single-operator by design.

## 7d. Phase 2 review findings closed

All three were reproduced before being fixed, and each has a regression test.

| Finding | Fix | Regression |
| --- | --- | --- |
| `safe_exception(ValueError("Authorization: Bearer canary-…"))` returned the canary intact: the generic key/value scrubber matched `authorization:` and redacted only the scheme word, leaving the credential. `Trace.blocked` then carried it to stdout, SQLite and the export. | Inline `bearer|basic|digest|token|apikey` schemes are scrubbed *before* the key/value pass, and arbitrary exception text is no longer logged at all — only allowlisted structured metadata (`SafeError`), otherwise the message is withheld. | `tests/test_redaction_canary.py`: Bearer and Basic canaries in inline text, checked in all three sinks, plus a withheld arbitrary `ValueError`. |
| `verdict("S2", {"save_first": ""}, [])` reported `harness_healthy=True`, and the exporter silently skipped blank trace ids. | Required trace *and* assertion maps per scenario; blank/missing ids and missing contract assertions are `missing_evidence` → unhealthy; the reset must be proved by its recorded assertion (`fixture_reset_reaches_the_documented_starting_state`, `holds=true`), not by a redirected HTTP 200; N1 must contain a real `expected_denial`; the CLI exits non-zero. Known product failures stay separate and unfixed. | `tests/test_verdict.py` (7 cases). |
| Anonymous clients could select a `portal_profile` cookie, an unknown profile fell back to `analyst`, and state-changing routes had no CSRF. | Smallest authenticated demo gate on every route except `/healthz`; profile cookies signed with `itsdangerous`; a bad signature or unknown profile is a 400, never a fallback to the more capable identity; CSRF token on every write plus same-origin checks; demo roles still map to fixed server-side Superset credentials and the restricted 403 is preserved. Label changed from "Signed in as" to "Demo profile". | `tests/test_demo_gate.py` — each rejection asserts the status *and* that the event store gained nothing, so no rejected request leaves an event a repair job could be built from. |

## 7e. Phase 3 results — incident console and handoff export

### Model

`portal/incidents.py` keeps its own SQLite database (`/data/incidents.sqlite`)
fed by an observer on the event store, so an incident is only ever built from
events the server itself wrote — never from a browser-supplied field.
Observation is independent of `AUTO_REPAIR_ENABLED`, which gates external
dispatch only.

- **Failure families.** Two registered families: S2
  `discarded_form_data_key_is_reused` (three assertions — key reuse on save,
  the resurrected link on read, and the link-shows-its-own-state control) and
  S1 `omitted_row_limit_is_reset`. Sibling assertions map to one incident;
  the assertion name, route, trace and step survive as evidence.
- **Fingerprint.** `sha256(target_repo, verified baseline SHA, family, actor)`,
  truncated to 32 hex chars. Times, trace/request/event ids, form-data keys and
  chart ids are excluded by construction. An environment whose running code is
  `unmeasured` or does not match its checkout fingerprints as `unverified`, so
  it cannot merge with the verified baseline.
- **Delivery vs occurrence.** `incident_events.event_id` is a primary key
  (duplicate delivery is a no-op inside `BEGIN IMMEDIATE`);
  `incident_occurrences` is keyed `(incident_id, trace_id)`, so an occurrence is
  one *failed user action*. The console labels incidents, failed actions and
  evidence events as three separate numbers.
- **Never an incident.** Expected N1 denials (two denial events from one
  attempt still produce zero), `blocked`/`error` outcomes, unregistered
  assertions and the portal's own harness checks. Events from a
  `preview`/`reproduction`/`verification` environment attach to the parent named
  by `PORTAL_PARENT_INCIDENT` (server configuration, operator-set) and add
  evidence only — without a configured parent they are suppressed and recorded
  as a processing error, which is what stops a repair session's own
  reproduction from opening a second repair job.
- **States.** `detected` → `candidate_fix` → `verified_in_preview`; nothing
  self-promotes. Issue, session, PR and verification fields read
  `not connected` because no integration exists yet.

### Export

`POST /ops/incidents/{id}/export` writes `incident.json`,
`events.redacted.jsonl`, `reproduction.md` and `manifest.json` from stored
evidence. The manifest lists the actual files with byte sizes and SHA-256,
plus run id, environment kind, measured provenance (running-code hash, image
id) and the repository/baseline SHA. `reproduction.md` carries the executable
repository + fixture setup and the exact same-tab steps. Events are scrubbed a
second time on the way in, because the bundle leaves the portal. No ZIP.

### Verified in Phase 3

| Check | Result |
| --- | --- |
| `python3 -m pytest tests -q` | 69 passed (24 new incident/bundle/route tests). |
| `python3 -m flake8 portal scenarios scripts tests` | Clean. |
| Repeated identical event | `duplicate_event`; incident, occurrence and event counts unchanged. |
| New matching failed action | Occurrence +1, exactly once. |
| Sibling assertions in one action | One occurrence, two evidence events. |
| Concurrent duplicate ingestion (8 threads) | 1 stored, 7 `duplicate_event`. |
| Restart recovery | Counts survive reopening the database *and* the container rebuild; a pre-restart event replays as a duplicate. |
| HTTP 200 semantic failure | Creates the incident (outcome stays `assertion_failed`; no fabricated 5xx). |
| Expected 403 / blocked / error / unregistered / harness | Suppressed, zero incidents. |
| Preview event without a parent | Suppressed + processing error; with a configured parent, evidence only (`failed_actions` unchanged). |
| Unmeasured baseline | Separate `unverified` incident, not merged. |
| Export parsing + checksums | Four files written, JSON/JSONL parse, every SHA-256 recomputed and matched. |
| Credential canary through the bundle | Absent from all four files. |
| Console authorization | List, detail, file download and export are operator-only (401 anonymous and as the demo user); export also requires CSRF (403 without). |
| Live replay | `scripts/export_examples.py --out artifacts/phase3/examples` → S2 6 actions/31 events, S1 3/25, N1 3/20, harness healthy in all three; console shows **2 incidents / 3 failed actions**, N1 contributing none. |

### Evidence

- `artifacts/phase3/examples/{S1,S2,N1}/` — replay events and verdicts.
- `artifacts/phase3/handoff/<fingerprint>/` — the four-file bundles as written.
- `artifacts/phase3/screenshots/` — incident console and browser demonstration.
- Phase 1 and Phase 2 artifact paths are untouched.

### Not covered in Phase 3

- No issue, session, PR or verification data: those fields stay
  `not connected` until Phase 4/5 wire real integrations.
- No repair dispatch of any kind; `AUTO_REPAIR_ENABLED` stays `false`.
- The preview/parent path is tested deterministically, but no preview
  environment has actually been deployed yet (Phase 5).

## 7f. Phase 4 results — GitHub/Devin controller (offline)

Everything below was exercised against `portal.simulation` fakes in an
isolated simulation database. No GitHub issue, Devin session or message was
created, and no credential was requested or used.

### What the controller does

| Concern | Behaviour |
| --- | --- |
| Trigger | An eligible incident reaching the store is considered immediately; nobody opens the console and nobody tags an issue. With dispatch off the exact issue body and Devin request body are persisted and shown in protected incident detail. |
| Single flight | `repair_slot` is a one-row table. `INSERT OR IGNORE` decides the winner, so two `RepairStore` connections, two workers or a restarted process all contend on the database rather than on a read-then-act `active()` check. The claim is taken before the first remote write and covers creation, dispatch, candidate and verification. |
| Releasing the claim | Only when nothing can still be running: a confirmed termination, or an agent that finished with no product candidate. An ambiguous or refused termination, an ambiguous create and any unread session state keep the claim and leave `needs_attention` visible. |
| Budget | ACU and wall-clock are checked *before* anything is sent, including before a follow-up message. The deadline starts at activation — a proposal written while dispatch is disabled carries an empty deadline, so a live run does not begin already expired. Budget stops are a visible terminal reason. |
| Follow-ups | At most two, counted durably, never sent past the budget. A candidate is not terminated before verification can return to it, because a terminated v3 session cannot resume. |
| Pull requests | Before a repair becomes a candidate, GitHub — not the agent — must report the allowed host and repository, base `runtime-repair/baseline`, a head branch inside `woohyeokk-choi/superset`, a full 40-character head SHA, and an open, unmerged pull request. Candidate is still not success. |
| Reconciliation | Creation intent is persisted before the remote write. Issue reuse matches the exact incident marker and skips anything carrying a `pull_request` field; session reuse requires the requested tag to actually be present. Ambiguous writes park for a human instead of retrying. |
| Transport | Only a connection that was demonstrably never opened (`NewConnectionError`, `NameResolutionError`, `ConnectTimeout`) is `Refused`. A reset, a drop or an adapter-wrapped `ProtocolError`/`OSError` around the write is `Ambiguous`, because the server may already have acted. |
| Off the request path | `RepairPoller` resolves stale creation claims and polls the claimed repair on a timer. A 401/403/429 or an unreadable session becomes persisted `needs_attention`, not an exception in a customer request. |
| Providers | `AUTO_REPAIR_ENABLED=true` builds the real clients from configured credentials; missing or malformed credentials raise `NotConfigured` at start-up. There is no fake fallback. Disabled mode constructs no provider and reads no credential. |

### Tests

`python3 -m pytest tests -q` → **144 passed**; `python3 -m flake8 portal
scenarios scripts tests` clean; `python3 -m compileall -q portal scripts
scenarios tests` clean.

| Check | Result |
| --- | --- |
| Two distinct incidents, two store connections, concurrent `consider()` | One `dispatched`, one `deferred`; the shared fake Devin holds exactly one session. |
| Restart | The claim is still held by the same repair after reopening the database; a different incident is deferred. |
| Termination outcome unknown | `terminal` with `session may still be running`; the claim is *not* released. |
| Follow-up past the deadline / past the ACU limit | `stopped`, no message sent. |
| Deadline start | Empty while proposed; set to activation + wall-clock minutes at dispatch, six hours after the proposal was written. |
| Pull request head in `untrusted-owner/untrusted-fork`, short SHA, empty SHA, closed, merged | All parked; `pr_head_sha` stays unset. |
| Transport | `NewConnectionError`/`ConnectTimeout` refused; `ProtocolError`, `ConnectionResetError`, `OSError`, bare `ConnectionError` and `ReadTimeout` ambiguous. |
| Poll 401 / 403 / 429 | Persisted `needs_attention` with the status, claim held. |
| Creation claim whose worker died | Settled `ambiguous` and parked for a human; never recreated. |
| Session without the requested tag / issue that is a pull request | Not adopted, not reused. |
| Providers | Disabled builds none; enabled without credentials raises; enabled with injected fakes dispatches. |

### Fresh-host bootstrap evidence

A clean checkout in an isolated Compose namespace (`bootstrapcheck`, ports
8188/5108/8190, its own network and image) seeded 600 rows, served
`/healthz` → `{"status":"ok","environment_kind":"baseline-light"}` and
reproduced S2. The seed command refuses a project it was not pointed at.
**Caveat:** the MCP container in that namespace reported `unhealthy` even
though the S2 scenario and portal health both succeeded — the stack was
usable, not entirely healthy. Recorded in `artifacts/phase4/bootstrap/`.

### Not covered in Phase 4

- No live GitHub or Devin call of any kind, and no credential in use.
- Verification is not implemented: a repair can reach `candidate`, never
  `verified_in_preview` (Phase 5).
- Pull-request path and diff checks are Phase 5; only URL, repository, base,
  head SHA and open/merged state are validated here.
- The MCP health caveat above is unexplained.

## 7g. Phase 5 results — independent verification (offline)

### Review gaps closed

| Gap | Resolution |
| --- | --- |
| Dispatch on the HTTP request path | A request now persists the event, admits the incident and writes a **proposal**. `Controller.consider()` touches no network. `RepairWorker.tick()` → `Controller.advance()` settles dead creation claims, walks the repair holding the slot (dispatch / poll / verify), and claims the oldest queued proposal when the slot frees. Proven with injected slow fakes: the request returns while `create_session` is still sleeping, and a second incident queued behind the first starts on the next tick with no further browser action. |
| Phase 4 bootstrap called "clean" while its own provenance said `checkout_dirty=true` | The historical evidence and its `provenance.json` are unchanged; `artifacts/phase4/bootstrap/README.md` carries a correction. New evidence in `artifacts/phase5/bootstrap/` is a fresh clone of pushed automation code (`c7ba960`, clean) and Superset at `394bca5` (clean), in its own Compose project `phase5check` on ports 8288/5208/8290. |
| MCP container `unhealthy` | The sidecar inherited the *web* image's healthcheck (`docker/docker-healthcheck.sh` → `/health`), a route the MCP listener does not serve. The overlay replaces it with an MCP `initialize` against `127.0.0.1:5008/mcp` requiring `serverInfo`. The container reports `healthy`. Independently, `portal.validator.mcp_readiness` speaks `initialize` + `tools/list` before S1 runs: server `Superset MCP Server 3.4.7`, protocol `2025-06-18`, tools `get_instance_info, health_check, search_tools, call_tool`. |
| Restricted 401 treated as an expected denial | `SupersetGateway` classifies 401 as `blocked` with structured `authentication_failure` metadata, and only 403 from the restricted profile as `expected_denial`. The N1 validator case blocks on any 401 and on any 5xx, since neither answers the authorization question. |
| Provenance strength claimed rather than measured | `portal/measure.py` measures each service the verification actually uses: container/image/compose identity, the hash of the Python tree *inside* the container, the host path its mount points at and that tree's hash, checkout SHA and dirty flag, config-file revision, and a fixture digest over the region/channel/product revenue aggregate the chart-data API returns (its row count is that grouped projection, not the seeded records), plus `measured_at` and the selected automation ref. A candidate whose measured source hash or SHA disagrees with the head under test is blocked, not passed. |

### The verification loop

| Stage | Rule |
| --- | --- |
| Candidate metadata | Re-read from GitHub: allowed host, target repository, head repository inside `woohyeokk-choi/superset`, base `runtime-repair/baseline`, open and unmerged, full 40-character head SHA. An agent-supplied URL or SHA is never sufficient. |
| Change scope | Changed paths are judged **before** anything is built. Automation, validator, fixtures, authentication/CSRF, workflows, dependencies and Docker bootstrap are forbidden. A scope rejection is `blocked`, not `failed`: no candidate code ran, so there is no product evidence to feed back. |
| Execution | `portal/isolation.py` checks the exact head SHA into a candidate-only checkout and brings up its own Compose project, ports, database, cache, volumes and network. No host Docker socket, no controller/GitHub/Devin credential in the environment or the mounts (the whole-stack mount was narrowed to the single non-secret `superset_config_mcp.py`), and a canary check refuses to start a stack carrying any known secret name. |
| Assertions | Imported from `portal.validator` at a pinned automation revision inside the trusted checkout, never from the candidate. |
| Verdict | `passed` only if every registered target and control check ran and held; `failed` only if the product contract was exercised and did not hold; everything else — setup failure, empty trace, skipped check, missing assertion, transport error, stale or moved head, missing/mismatched provenance — is `blocked`. The head is re-read afterwards so a newer commit cannot inherit an older pass. A pass becomes `verified_in_preview`; it is not deployed, merged or production-ready. |
| Feedback | A product-contract failure sends the precise expected/observed lines to the **same** session, once per candidate SHA, at most two follow-ups, and only after the ACU and deadline checks. Infrastructure failures ask for attention instead. |
| Slot | Released only when nothing can still be running. Verified or terminal with a confirmed termination releases and the next queued proposal advances; an ambiguous termination keeps the claim and stays visible. |
| Simulated vs real | Simulated attempts are stored with `simulated=1` and excluded from `verified_count()`, which counts `verdict='passed' AND simulated=0`. |

### Negative control — the validator against the immutable baseline

`artifacts/phase5/negative-control/` (real validator, published automation
`449edb3`, isolated baseline stack):

| Report | Exit | Verdict |
| --- | --- | --- |
| `baseline.json` | 1 | failed — S2 and S1 target contracts |
| `blocked-web-unreachable.json` | 2 | blocked |
| `blocked-mcp-unreachable.json` | 2 | blocked |
| `blocked-bad-credentials.json` | 2 | blocked |

```
S2.new_exploration_does_not_reuse_a_discarded_key: expected False, observed True
S2.discarded_link_stays_dead_after_a_new_exploration: expected 404, observed 200
S1.an_omitted_row_limit_keeps_the_saved_value: expected 137, observed 1000
```

Every control held: a second workspace gets its own key and state; a
same-context save without discarding updates in place; the requested sort
change persisted; the palette survived; explicit row limits (including `1000`,
which is indistinguishable from a reset unless checked) were applied; a new
chart kept the schema default; and the authenticated Gamma user listed charts
(200) while being refused both the exploration-state write and chart data
with 403.

### Verifier acceptance gaps closed after review of `c7ba960`

| Gap | Resolution |
| --- | --- |
| `Verifier.verify` returned `passed` for a report of two duplicated `WRONG_CASE` entries whose only check was an unregistered failing control | The verdict is derived, never read. `portal/verification.py` grades each requested case against `REQUIRED_CHECKS`: exact registered identities, no duplicates, no foreign cases, every required assertion present exactly once, a known `kind`, a boolean `holds` on every graded check. A report whose own summary disagrees with the derived result is `blocked`, as is a malformed or empty `failed` report — which therefore cannot buy paid feedback either. The regressions drive `Verifier.verify()` itself, not `Recorder.result`. |
| `provenance_problem` accepted `source_mount="/tmp/candidate-other/superset"` for `checkout="/tmp/candidate"`, `clean` omitted, `measured_at="not-a-time"` and no container measurements at all | Fail-closed throughout: mounts are compared by resolved path ancestry (a lookalike sibling is rejected), `clean` must be exactly `True`, `measured_at` must parse, be no older than 6 hours and not be in the future, both web and MCP must report container id, image id, `running` state, a non-`unhealthy` health, a mount inside the candidate checkout, a source SHA equal to the tested head, and a `code_hash` equal to the trusted checkout's. Automation ref, config revision and fixture content must be present. An absent measurement is never a true one. |
| `check_scope(["superset/config.py"])` was allowed, since every `superset/` and `tests/` path was | Scope is per failure family (`SCOPE_BY_FAMILY`): the form-data/key-value tree for the discarded-key defect, the chart/MCP tree for the row-limit defect. Global configuration, app startup, initialization and authentication, CI, Docker bootstrap, dependencies, fixtures, the validator and the automation repository are rejected outright, and an unregistered family falls back to review-required. A test-only diff is rejected too. This is a path boundary and nothing more: it does not prove the change is semantically safe. |
| `IsolatedStack._seed` omitted the Compose namespace, so `seed_synthetic.py` defaulted to the baseline `superset` project | `_seed` passes `SUPERSET_COMPOSE_PROJECT`, `COMPOSE_PROJECT_NAME` and the derived `-db-light-1` / `-superset-light-1` container names. The restricted Gamma user is created idempotently by the published seed (`ensure_restricted_user`), which N1 imports rather than duplicating; no manual rescue command is involved. A failed preparation takes down only that candidate project and removes only its checkout. |
| The Dockerized portal has no git or Docker CLI, yet the in-app worker builds `IsolatedStack` from host subprocesses | `portal/coordinator.py` documents and implements the split: the portal container observes, records incidents and writes proposals with `AUTO_REPAIR_ENABLED=false` and no git, Docker or repair credential; a trusted **host coordinator** with `AUTO_REPAIR_ENABLED=true` owns git, Docker, the candidate workspace and the controller credentials, and drains the same durable SQLite state. `python3 -m portal.coordinator check` reports what the hosting process can actually do (`can_verify`), so the portal's honest answer is `false` rather than a runtime crash. Candidate containers still get no socket and no credential. |

### Integrated baseline run through `IsolatedStack.prepare()`

`artifacts/phase5/integrated/` — the coordinator ran the real preparation path
against the immutable baseline and then the real validator through it, from a
fresh clean checkout of published automation `58ab5be`:

- Compose project `candidate394bca55c792`, ports 8388/5308, its own checkout,
  database, cache and volumes; the baseline stack was untouched.
- `provenance_problem` empty: clean checkout at the tested commit, equal
  `code_hash` on the host and inside both containers, MCP `healthy`, fixture
  digested through the chart-data API as 60 grouped rows — the
  region/channel/product revenue aggregate of the 600 seeded records, not the
  records themselves.
- Exit code 1 — a product-contract failure, not a blocked run: the same three
  failing target assertions, with every setup and control check holding and N1
  passing. They come from **two** defects: S2's discarded form-data key shows
  up twice, on save and on the stale link, and S1's row-limit reset once.
- Cleanup left no candidate container, volume or workspace directory.

Two real automation defects surfaced only because this path was actually run:
`find_dataset` read one page of `/api/v1/dataset/` and missed the fixture in a
stack that also carries the example datasets (provenance correctly blocked
rather than validating an unmeasured fixture), and teardown could not delete a
checkout whose bytecode the container had written as root. Both are fixed.

### Deployment wiring: one directory, one queue, whole traces

| Gap | Resolution |
| --- | --- |
| `coordinator.run` opened `IncidentStore` without its event log, so `IncidentStore.get` returned `trace_events={}` and a coordinator-built handoff carried failing assertions with no request sequence | `portal.coordinator.open_state` opens all four stores as one thing, attaches the shared `EventStore` and drains it (catch-up covers an observer that died mid-write); `build_worker` is what `run` itself uses, so the production path and the tests share the wiring. Its `providers` argument lets injected clients travel that same path. |
| `stack/docker-compose.portal.yml` mounted the named volume `portal_data:/data` while the coordinator documented a shared host directory — the portal's queue and the coordinator's could silently be two queues | The mount is `${PORTAL_DATA_DIR:?...}:/data`, required rather than defaulted, and the named volume is gone. Both processes write those SQLite files, so the container runs as `${PORTAL_UID:-10001}:${PORTAL_GID:-10001}`: files created 0644 by one uid are read-only to the other. The portal's `AUTO_REPAIR_ENABLED` is fixed to `false` in the Compose file instead of passed through, so sourcing a shared `.env` cannot turn the container into a second dispatcher. Credentials stay in the coordinator's environment and no Docker socket is mounted. |

Enabling dispatch also needs two things on the target fork that the
controller does not create: **Issues** enabled, and a `runtime-repair` label
for the issues it opens or reuses. Both are in place on
`woohyeokk-choi/superset`; another fork needs them created first.

`artifacts/phase5/wiring/` — the real portal image, `--network none`, running
as the coordinator's uid, recorded an upstream call, a failed assertion and a
`proposed` repair in the bind mount; the coordinator's own `build_worker`
claimed it and dispatched it to simulated providers. `trace_events` came back
`{"shared-trace": 2}` and the Devin prompt carried
`superset.save_exploration` with its status and body summary and no
missing-trace gap. `scripts/check_shared_state.py` reruns it and refuses a
directory that already holds state, because the dispatch is simulated.

### Tests

`python3 -m pytest -q` → **233 passed**; flake8 and compileall clean over
`portal scenarios scripts tests`. `tests/test_deployment.py` holds the
in-process half: the portal's queue read through the coordinator's wiring, the
detached `IncidentStore` that loses the trace, and a coordinator pointed at a
different directory finding nothing.

Beyond the Phase 4 set: a customer request that returns while the provider is
still sleeping; the next queued incident starting without a browser action;
the queue surviving a restart; candidate SHA and mount-provenance mismatch;
missing assertions and empty evidence; a foreign head repository; no
credential reaching the candidate environment; forbidden path scopes
(automation, validator, fixtures, auth, workflows, dependencies, Docker);
setup failure; a head that moved during the run; duplicate feedback
suppression for the same SHA; the two-follow-up cap; ambiguous termination
keeping the claim; a simulated pass excluded from the real verified count;
and the 401 vs 403 split.

### Not covered in Phase 5

- **No candidate stack has been built from repaired code**, because no repair
  PR exists. `IsolatedStack.prepare()` itself has now been run for real, but
  against the baseline commit; the pull-request read that normally precedes it
  is still exercised only through fakes.
- The verification runner has only ever been driven by the host coordinator.
  The Dockerized portal reports `can_verify=false` by design, and a deployed
  container that verifies is not claimed.
- No repaired code, therefore no passing target contract anywhere. The first
  genuine pass must come from an API-created repair PR.
- No live GitHub issue, Devin session or message; no credential read;
  `AUTO_REPAIR_ENABLED=false` throughout.

### What Phase 6 still needs

1. The funding/credential decision: `DEVIN_API_KEY`, `DEVIN_ORG_ID`,
   `GITHUB_TOKEN`, and confirmation that API-created sessions can push to
   `woohyeokk-choi/superset`.
2. Approved numeric ACU and wall-clock limits.
3. A first live dispatch with the flag on, then a real candidate PR run
   through the verifier on a machine with Docker headroom for a second stack.

## 7h. Phase 6 results — the two live pilots

### Phase 6 preflight (read-only, no live creation)

Run at `d4a0785` plus the session-id fix below. Nothing was created: no issue,
no session, no message, no paid call.

| Check | Result |
| --- | --- |
| Devin v3 auth (`cog_` service-user key, `org-6636418ab2fc4768997020264518843c`) | `GET /sessions` 200; documented `first`/`items` pagination answers |
| Sessions tagged `runtime-repair` | none — reconciliation starts from an empty set |
| `max_acu_limit` in a session read | absent; the API does not report a per-session cap back, so the limit is only what the create body asks for |
| GitHub reads on `woohyeokk-choi/superset` | issues 200, pulls 200, `runtime-repair/baseline` = `394bca55c792b7b3547e23f6e175a7cb0f0757e8` |
| GitHub writes (issue create, automation branch push) | unproven by reads; `GET /user` is 403 for this app credential and repository `permissions` report false. Not inferred from a successful read |
| Coordinator host capability | `python3 -m portal.coordinator check` with `PORTAL_DATA_DIR`, `PORTAL_AUTOMATION_DIR`, `PORTAL_VERIFICATION_WORKSPACE`, `PORTAL_BASELINE_SHA` → `can_verify=true`, `dispatch_enabled=false` |
| Shared-state handoff at published code | `scripts/check_shared_state.py` over a scratch directory: portal container proposes, host coordinator claims, `trace_events {"shared-trace": 2}`, prompt carries the request/response summary |
| Baseline services | web `/health` 200, portal `/healthz` 200, MCP answers; the baseline project's MCP container still shows Docker `unhealthy` because it was created before the overlay probe fix — candidate stacks use the fixed overlay |

One preflight blocker was found and fixed: the v3 API returns a bare
32-character session id, and `Devin.find_tagged` only recognised the
`devin-` spelling, so reconciliation after an ambiguous create would have
missed the session it had just paid for and opened a second one. Both
spellings are now accepted and nothing else is
(`test_a_live_shaped_session_id_is_adopted_after_an_ambiguous_create`).

Both permission questions were answered by the first authorised write rather
than by a probe: the controller's own issue creation succeeded, and the
Member service user created the session.

### Phase 6 live S2 pilot

Published automation revision `7b88b9e`, clean tree, fresh state directory
`runtime/live-state` (0700, empty before the run), portal started under the
`live` Compose project with `AUTO_REPAIR_ENABLED=false`.

| Step | Result |
| --- | --- |
| Real portal action (demo gate, signed profile, CSRF, one tab) | saved an exploration, discarded it, saved another: the second exploration received the discarded key and the discarded link resolved again |
| Incident | `discarded_form_data_key_is_reused`, fingerprint `f7ff733a667fe52ceddc3eb0cff204c1`, admission `eligible`, 2 occurrences |
| Coordinator `check` on the host | `can_verify=true`, `dispatch_enabled=true` |
| GitHub issue (first write, through the controller) | <https://github.com/woohyeokk-choi/superset/issues/1> |
| Devin session (exactly one) | `4b7011c76c1e4ec8bf28cb373b614577`, `max_acu_limit=20` in the create body, deadline two hours after activation, tagged with the fingerprint and `attempt-1` |
| Handoff | the prompt carries the recorded `portal.save_exploration` request with its status and body summary, the baseline SHA and the issue link |

The GitHub credential is resolved per request from the host's `gh`, so a
two-hour repair outlives a one-hour installation token; `GITHUB_TOKEN` still
takes precedence where a deployment sets one. Neither credential reaches a
candidate stack.

### Independent verification of the candidate

| Step | Result |
| --- | --- |
| Candidate PR | <https://github.com/woohyeokk-choi/superset/pull/2>, base `runtime-repair/baseline`, head `d234055eaf85a70d5e5ec5a7a7256ee43d02dde6` read from GitHub |
| Blocked attempts preserved | scope (test tree outside the S2 family), port `8288` held by an older stack, and a candidate checkout deleted mid-run by concurrent coordinator loops — recorded as `blocked`, never converted into product feedback |
| Concurrency defect | four `portal.coordinator run` loops shared one state directory and each `IsolatedStack.prepare` removed the other's checkout and Compose project; `run` now holds an advisory lock on `$PORTAL_DATA_DIR/coordinator.lock` |
| Verdict | `verified_in_preview` on attempt 8: S2's two target assertions plus the second-workspace and same-context controls, and N1's authenticated 403, all hold against the candidate stack; provenance clean with the in-container code hash and a 60-row fixture content digest |
| Feedback sent | none — the candidate passed on first behavioural replay, so no follow-up was spent |

Two observations about limits, recorded apart from each other because they
measure different things. The API reports no per-session cap on reads, so
`max_acu_limit=20` is what the create body asked for and the controller's own
deadline and usage gates are what actually stop work. Separately, the session's
native usage panel showed an on-demand limit *since last message* of `$20` with
`$0` consumed at inspection (read-only, nothing saved) — a per-message UI
setting, not a whole-session cap and not a conversion between ACUs and dollars.
Native Slack sync for that session is unavailable to anyone but its owner, the
`superset-runtime-repair` service user; it stays deferred rather than being
obtained by changing ownership or duplicating the repair.

### Phase 6 live S1 pilot

Published automation revision `86c03c3`, clean tree, same `runtime/live-state`
directory with all S2 records preserved, one singleton-locked host coordinator,
portal still `AUTO_REPAIR_ENABLED=false`.

| Step | Result |
| --- | --- |
| Fixture restored through `POST /ops/fixtures/reset` | chart 6 read back at row limit `137`, palette `googleCategory10c` |
| Real portal action (`POST /settings/sort`, no `row_limit` sent) | read back row limit `1000`, palette unchanged, sort changed as requested |
| Control | a restricted-viewer action in the same window returned its expected authenticated denial and created no incident |
| Incident | `omitted_row_limit_is_reset`, fingerprint `4850806e18c18de5acf83d2588df81c0`, admission `eligible` |
| GitHub issue | <https://github.com/woohyeokk-choi/superset/issues/3> |
| Devin session (exactly one) | `18b04127f4a44af6a9c71f9eb3eaba9e`, `max_acu_limit=20` in the create body, deadline 22:44 UTC, service-user attribution |
| Candidate PR | <https://github.com/woohyeokk-choi/superset/pull/4>, head `fe266eac51a996760a75997ff94b3270c9ef73b1` read from GitHub |
| Verdict | `verified_in_preview` on attempt 1 (verification 9): 13 checks over S1 and N1, including an explicit `1000` still being honoured and creation defaults unchanged |
| Feedback sent | none — no product check failed |

S2's defect is not required to pass this candidate: PR #2 is open and unmerged,
so that bug remains in the baseline this branch starts from, and each repair is
judged against its own registered cases. Evidence in
`artifacts/phase6/S1/`.

## 7j. Phase 7 results — handoff, simulation and console QA

No live repair, verification or paid session ran in this phase; the coordinator
stayed stopped and both live states were preserved untouched.

| Deliverable | Result |
| --- | --- |
| README | rewritten from the actual implementation: the working code is on `devin/1789830208-phase1-baseline-reproductions` (PR #1), not `main`; bind-mounted state and UID/GID created before Compose; host-only credential/git/Docker boundary; exact log, ops and export locations; `python3 -m portal.coordinator check`; repair-slot dedup vs per-state coordinator lock; cleanup scoped to this project |
| Simulation | `scripts/check_shared_state.py` from a clean clone at `79c9c94`, image `sha256:64da1872be7d…`, `docker run --rm --network none` with `FakeDevin`/`FakeGitHub`, printing `shared state check: PASS`; it refuses to run against a state directory that already holds live databases (exit 2). Evidence in `artifacts/phase7/simulation/` |
| Documents | `docs/results.md` (both cases, exact links, S2 19 checks / 7 blocked + 1 pass, S1 13 checks / 1 pass, 0 product follow-ups), `docs/submission.md`, `docs/demo-script.md` (one continuous take, 4:45 target, 5:00 ceiling) |
| Console QA | real traces, request/response steps and sanitized input/output; `/ops/export.jsonl` returned 190 valid JSONL events matching no container secret; a repeated baseline S1 observation produced no third incident, repair or session; counts stayed at 2 incidents / 2 repairs / 2 sessions; `AUTO_REPAIR_ENABLED=false` and no UI control can start a repair |

QA found two real console defects, both fixed and regression-tested:

1. The incident list and detail read state and links from the incident row,
   which only ever records detection — so pages showing a verified pull request
   also said `detected` and `not connected`. `lifecycle()` in
   `portal/controller.py` now projects the repair row, which is the only writer
   of issue, session, pull request and verdict.
2. A verdict was shown without its assertions, and the attempt table was wide
   enough to need heavy horizontal scrolling. Attempts now render per case with
   every check's name, kind, expected, observed and `holds`, link to the stored
   report at `/ops/verifications/{id}/report`, and wrap instead of overflowing.

The checks shown are the same stored records the grader used: 19 for S2
(16 S2 + 3 N1) and 13 for S1 (10 S1 + 3 N1). N1 is shared, so the two runs are
not 32 unique tests, and setup and control checks are counted alongside target
assertions.

The candidate stacks were destroyed after validation, so the console shows
stored verification evidence, not a live preview.

### Phase 7b — outbound Slack status notifier

`portal/notify.py` publishes one line per real lifecycle transition (session
started, PR available, verification passed/blocked/failed, follow-up sent,
needs-attention, terminal stop) to a single incoming webhook held only by the
host coordinator process. Polling and queueing are silent, and an expected
authenticated 403 is not an incident, so it cannot alert. Messages escape
Slack markup, disable unfurls and mentions, and carry the case, incident and
repair ids, the real issue/session/PR links, the exact tested SHA and
"verified in isolated preview; not merged/deployed".

Delivery is a SQLite ledger (`$PORTAL_DATA_DIR/notifications.sqlite`) with
stable event ids, at most 4 attempts and 0/30/120/600s backoff, visible at
`/ops/notifications`. Ambiguous transport outcomes are recorded `unknown` and
never retried: **delivery is not exactly-once**, and a retry can duplicate. A
missing or invalid webhook records `disabled` and makes no request; the URL is
validated (`https`, `hooks.slack.com`, `/services/…`), redirects are refused,
and `portal.redaction` scrubs webhook-shaped strings from logs, errors and
exports. Nothing here can change a repair outcome or start a session: the
worker announces only after the controller has written its decision, and
notifier failures are swallowed into the ledger.

**Five real messages reached `#superset-alerts`, not the three that were
authorized.** The three authorized ones are the connectivity test
(`ts 1789854442.246559`) and the two historical backfills
(`ts 1789854458.454909`, `ts 1789854458.664169`), marked "Historical result —
repair ran earlier" and read from the stored repair/verification records
without touching them; re-running the command sends nothing.

The two unintended ones (`ts 1789854423.113499`, `ts 1789854433.643299`) were
simulated lifecycle lines naming `simulated-repo` issue 1 and session
`simulated-1`, posted by the test suite. Root cause: `build_worker()`
constructed `build_notifier(config)` unconditionally, and `build_notifier`
reads the ambient `SLACK_WEBHOOK_URL`, so a worker wired with FakeGitHub and
FakeDevin still held a real `HttpTransport` to the production-like incident
channel. Hiding the environment variable in one test would not have been a
fix — the process could hold the credential for any other reason.

The fix refuses the real transport at runtime. `Transport` now carries a
`simulated` marker (`HttpTransport` false, `FakeTransport` true) and
`portal.transport.is_simulated` fails closed: a transport that does not
declare itself counts as simulated. `build_worker()` passes
`simulated=_is_simulated(providers)` and a simulated `Notifier` drops the
webhook before any code can read it, records `simulated repair: real delivery
refused`, and prefixes deliberately sandboxed messages `[SIMULATED]`. The
regression in `tests/test_deployment.py` sets a **fake canary** webhook,
patches `socket.connect`/`connect_ex`/`create_connection`, runs the simulated
dispatch and asserts zero outbound connections and no canary anywhere in the
ledger; `tests/conftest.py` also clears the ambient variable as a second
layer. The channel history is preserved as-is; nothing was deleted or edited,
and no further real message has been sent.

The message content follows the incident-response contract rather than the
internal state names. The first line is *"Suspected defect — investigation
started"* — an eligibility rule ran, nothing has reproduced anything — and
carries the failing action, expected versus observed, trace id, baseline SHA,
occurrence count, why the case was admitted, and the bounded plan. A pull
request is announced as *provisional*, quoting the session's own
reproduction claim as the session's claim. Only the independent replay of the
exact head produces *"verified in isolated preview; not merged/deployed"*,
with the attempt it came from. Blocked and failed validations say explicitly
that the repair is **not completed** and what happens next; neither consumes a
new session. A recording link appears only when one is supplied to the CLI
(`--recording`), so no video is ever implied, and only when the capture
states its own case, full revision and capture time (`--recording-case`,
`--recording-sha`, `--recording-at`): the stated revision is compared to the
accepted head, never inferred from it, and anything else is shown as
`recording pending` with the reason.

A `correction` command publishes one factual correction of something the
channel was already told, keyed by the correction's own wording so repeating
the command sends nothing. Channel history is never edited or deleted.

Native conversational Slack sync remains owner-only for service-user sessions
and is not used or claimed here.

## 8. Blockers and required credentials

Nothing is stored in this file; all values go into session/org secrets.

Credentials are requested at the moment they are needed, not up front. Neither
of the two below blocks Phase 1, and offline payload simulation in Phase 4 does
not require an API key.

1. `DEVIN_API_KEY` — needed only for live session creation (Phase 6). Phase 4's
   dry-run payload construction and validation runs fully offline.
2. `GITHUB_TOKEN` (repo scope on `woohyeokk-choi/superset` and
   `woohyeokk-choi/devin-automation`) — needed when the controller actually
   creates/reuses issues and reads PR state (Phase 4 live path onward).
3. Confirmation that Devin API-created sessions can clone and push to
   `woohyeokk-choi/superset` (repo must be connected to the Devin org).
4. ~~Unconfirmed reproduction of S1/S2~~ — resolved in Phase 1: both reproduce
   at the baseline commit (S1 narrowed to `row_limit`).
5. ~~Cold Docker build time unmeasured~~ — 1m 08s; not a constraint.

## 9. Standing constraints

- `AUTO_REPAIR_ENABLED=false` until explicitly flipped in Phase 6.
- Never edit, merge into or push to `apache/superset`.
- Never reset, force-push or replace existing branches or repositories.
- No production deployment; success stops at `verified_in_preview`.
- Credentials only in secrets — never in this plan, commits or logs.
- One phase per prompt; stop and report at each phase boundary.

### Restart recovery for alerts (review follow-up)

`_announce` only ever sees the `Decision` objects of the pass it is in, and
they are produced after `controller.advance()` has committed state. A process
that dies in that window loses a session-start or final-result message
permanently, because nothing revisits a decision.

`portal.notify.reconcile` closes it from the stored rows: on startup
`RepairWorker.catch_up` maps each repair's persisted state to the action it
implies (`dispatched`, `candidate`, `verified`, `parked`, `stopped`), builds
the same message under the same stable event id, and enqueues only ids absent
from the ledger. Constraints kept: current state only (no history replay),
nothing older than a one-off watermark written into `notification_meta` the
first time the ledger is consulted, no writes to repair or verification
records, no new dependency. `coordinator run` reports the recovered ids as
`recovered_notifications`.

Tests: `tests/test_notify.py` (recovery, watermark suppression, per-state
coverage, read-only, one bad row not silencing others) and
`tests/test_worker.py::test_a_dispatch_whose_process_died_before_speaking_is_announced_on_restart`
— a first worker commits a dispatch and says nothing, a second announces it
once, and neither a further restart nor the live pass repeats it.

### Exact-SHA local replay recordings (phase 8)

Both accepted repairs now have a short clip of the same user scenario failing
at the baseline and passing at the accepted head, recorded 2026-09-19
22:14–22:17 UTC, published under `artifacts/phase8/replay/` with each run's
`result.json`, sanitized `transcript.json` and environment manifest.

Method: `IsolatedStack.prepare()` built three scratch stacks on loopback ports
(baseline `394bca55`, S2 head `d234055e`, S1 head `fe266eac`); the unchanged
scenarios `scenarios/s2_form_data_key_reuse.py` and
`scenarios/s1_mcp_update_resets_fields.py` ran against them, with
`git rev-parse HEAD` of the running checkout shown before each run; all three
stacks were torn down afterwards.

Limits recorded with the evidence: the pinned light image has no compiled
frontend, so the chart editor could not be filmed and the footage is scenario
output plus the persisted REST read-back; N1 was not re-run; phase 6 records
are unchanged; no repair session, product edit, merge, deployment or Slack
send was involved.
