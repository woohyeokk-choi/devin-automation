# Devin Runtime Repair — Execution Plan

Single living plan. Updated at the end of every phase: decisions, phase status,
commands actually run with their results, and open blockers.

Last updated: Phase 0 (2026-09-19).

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
  `--profile mcp` instructions do not match the code here). The MCP server is
  started separately via `superset mcp run --host --port 5008`
  (`superset/cli/mcp.py`), and `fastmcp==3.4.7` appears only in
  `requirements/development.txt`, so the `dev` build target is required.
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
| 0 | Repo/branch state, feasibility assessment, this plan. | Plan committed; branch strategy agreed; blockers listed. *(in progress → report sent)* |
| 1 | Environment and scenarios: build the light stack, `superset init`, load synthetic data, script S1/S2/N1 as executable scenario definitions, confirm or refute each candidate by real reproduction. | Stack boots; S1/S2 reproduce (or are replaced); N1 returns 403 with no repair path. |
| 2 | Portal and logging: FastAPI portal over Superset, structured JSON logs from Superset config override + portal-side action logs, log intake into SQLite. | A failing user action produces a structured log record end to end. |
| 3 | Incident console: fingerprinting, dedup, state machine, operator console linking logs → incident → reproduction. | Repeated failures collapse into one incident, visible in the console. |
| 4 | GitHub + Devin integration: issue create-or-reuse on the fork, Devin API session creation with a reproduction-bearing prompt, session/PR tracking. Still `AUTO_REPAIR_ENABLED=false` — dry-run recorded, no paid sessions. | Controller produces the exact issue body and API payload it would send, stored and shown in the console. |
| 5 | Independent verification and bounded feedback: replay the scenario against the PR commit, record `verified_in_preview` / failure, return failures to the same session with the two-follow-up cap. | Verification runs against a simulated/PR commit and the cap is enforced. |
| 6 | Live API-created repairs: flip the flag for a controlled run; real session, real PR, real verification. | At least one incident reaches `verified_in_preview` from a live session. |
| 7 | README, evidence and Loom. | Evidence bundle and walkthrough complete. |

## 6. Phase status

- **Phase 0 — in progress.** Repos inspected, baseline confirmed, feasibility
  assessed, plan written. Awaiting review before Phase 1.
- Phases 1–7 — not started.

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

## 8. Blockers and required credentials

Nothing is stored in this file; all values go into session/org secrets.

1. `DEVIN_API_KEY` — required from Phase 4 (dry-run payload signing/validation)
   and mandatory for Phase 6. Not present.
2. `GITHUB_TOKEN` (repo scope on `woohyeokk-choi/superset` and
   `woohyeokk-choi/devin-automation`) — required for issue create-or-reuse and
   PR/commit status reads from the controller process. Not present.
3. Confirmation that Devin API-created sessions can clone and push to
   `woohyeokk-choi/superset` (repo must be connected to the Devin org).
4. Unconfirmed reproduction of S1/S2 — the largest technical risk; resolved in
   Phase 1.
5. Cold Docker build time for the Superset dev image is unmeasured and may
   dominate Phase 1.

## 9. Standing constraints

- `AUTO_REPAIR_ENABLED=false` until explicitly flipped in Phase 6.
- Never edit, merge into or push to `apache/superset`.
- Never reset, force-push or replace existing branches or repositories.
- No production deployment; success stops at `verified_in_preview`.
- Credentials only in secrets — never in this plan, commits or logs.
- One phase per prompt; stop and report at each phase boundary.
