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
- Phases 4–7 — not started.

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
| SQLite | `/data/events.sqlite` inside the portal container, on Docker volume `stack_portal_data` |
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
