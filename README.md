# devin-automation

Automation controller for the **Devin Runtime Repair** project: a synthetic-data
analytics portal backed by a fork of Apache Superset, where failing user actions
become deduplicated incidents that drive API-created Devin repair sessions and
independent verification.

Target repository: [`woohyeokk-choi/superset`](https://github.com/woohyeokk-choi/superset)
(baseline `394bca55c792b7b3547e23f6e175a7cb0f0757e8`).

The single living plan — decisions, phase status, commands and blockers — is in
[docs/EXECUTION_PLAN.md](docs/EXECUTION_PLAN.md).

Status: Phase 2 (portal + structured logging). No incident engine, no issue
creation and no repair sessions yet. `AUTO_REPAIR_ENABLED=false`.

---

## What runs here

| Piece | Where | Notes |
| --- | --- | --- |
| Superset light stack | `docker-compose-light.yml` + `stack/docker-compose.ports.yml` | Web on `127.0.0.1:8088`. The MCP sidecar is a development endpoint that authenticates as an admin user, so it is bound to `127.0.0.1:5008` and is never published. |
| Portal + operator viewer | `stack/docker-compose.portal.yml` | FastAPI, server-rendered Jinja, no frontend framework. `127.0.0.1:8090`. |
| Event store | Docker volume `stack_portal_data`, mounted at `/data` | SQLite at `/data/events.sqlite`; the identical safe JSON is also written to the container's stdout. |

The native Superset frontend is **not** built: at this revision the light stack
serves no compiled assets, and the REST/MCP paths the scenarios exercise do not
need it. The portal therefore renders real data and real chart settings itself
through the same REST/MCP clients the scenarios use — nothing is mocked.

## Start it

Two checkouts are needed — this repository and the Superset fork at the
baseline revision:

```bash
git clone https://github.com/woohyeokk-choi/devin-automation.git
git clone https://github.com/woohyeokk-choi/superset.git
git -C superset checkout 394bca55c792b7b3547e23f6e175a7cb0f0757e8
```

```bash
# from the automation checkout
cp stack/.env.example stack/.env        # then edit: checkout paths, ports,
$EDITOR stack/.env                      # SUPERSET_NETWORK for your namespace
set -a; . stack/.env; set +a            # the edited file, never the example

# 1. Superset + MCP sidecar (from the Superset checkout).
#    In an isolated Compose namespace also set COMPOSE_PROJECT_NAME and
#    SUPERSET_LIGHT_IMAGE=<project>-superset-light, because Compose names the
#    image it builds after the project.
cd "$SUPERSET_DIR"
docker compose -f docker-compose-light.yml \
  -f "$AUTOMATION_DIR/stack/docker-compose.ports.yml" \
  up -d superset-light superset-mcp-light
curl -fsS "$SUPERSET_BASE_URL/health"   # the HOST view: loopback, not a service name

# 2. host prerequisites, then the synthetic fixture (idempotent; it only
#    touches disposable fixture records). The seed, the provenance capture and
#    the scenarios all run on the host and use the loopback URLs above; the
#    SUPERSET_CONTAINER_* URLs in the same file are the portal container's view.
cd "$AUTOMATION_DIR"
python3 -m pip install -r requirements.txt
python3 scripts/seed_synthetic.py

# 3. measured provenance for the running containers
python3 scripts/capture_provenance.py   # writes runtime/provenance.json

# 4. portal + operator viewer
docker compose -f stack/docker-compose.portal.yml up -d --build
curl -s http://127.0.0.1:8090/healthz
```

Tests: `pip install -r requirements-dev.txt && python3 -m pytest tests -q`.

## Portal

| Route | Purpose |
| --- | --- |
| `/` | Synthetic revenue table, profile switcher, save / start-another exploration. |
| `/explorations/{key}` | Open a saved exploration link (the S2 stale-link surface). |
| `/settings` | Chart settings read-back and table-sort change (the S1 surface). |
| `/ops`, `/ops/traces/{id}`, `/ops/export.jsonl` | Operator event viewer, trace detail, JSONL export. HTTP Basic (`PORTAL_OPS_USERNAME` / `PORTAL_OPS_PASSWORD`). |
| `/ops/incidents`, `/ops/incidents/{id}` | Incident console: one row per failure family, with counts, revisions and trace history (operator-only). |
| `POST /ops/incidents/{id}/export`, `/ops/incidents/{id}/files/{name}` | Write and download the handoff bundle (operator-only, CSRF-protected). |
| `POST /ops/fixtures/reset` | Deterministic fixture reset (operator-only). |

Everything except `/healthz` is behind the demo gate (`PORTAL_DEMO_USERNAME` /
`PORTAL_DEMO_PASSWORD`): an anonymous client cannot pick a profile, reach
upstream or mint an event that an incident could later be built from. Profile
cookies are signed; one that fails its signature or names an unknown profile is
refused with 400 rather than falling back to the more capable `analyst`. Every
state-changing form carries a CSRF token and cross-origin submissions are
rejected. `tests/test_demo_gate.py` asserts each rejection *and* that the event
store gained nothing.

Every user action gets one `trace_id`; every upstream REST/MCP call inside it
gets a `request_id` and a monotonic `step_index`. Outcomes are exactly one of:

| Outcome | Meaning |
| --- | --- |
| `ok` | The contract held. |
| `assertion_failed` | The call succeeded at the transport level but the state is wrong. HTTP 200 with wrong state is **never** turned into a fabricated 5xx. |
| `expected_denial` | The permission system worked (N1). Nothing escalates. |
| `blocked` | Unreachable service, missing fixture or failed setup — never reported as "not reproduced" or "verified". |
| `error` | An upstream or tool call actually failed. |

### Two kinds of failure

A replay at this baseline is *supposed* to produce failed assertions, so the log
keeps them apart and so does every summary:

| Label | Meaning |
| --- | --- |
| baseline defect | A Superset contract this environment exists to reproduce (S2 key reuse, S1 row-limit reset). Marked `known_baseline_defect` on the assertion and shown as "known baseline defect — expected here". Superset stays untouched. |
| harness failure | One of the portal's own checks (`subject: harness`), e.g. the fixture reset not reaching its documented state. These are the only assertion failures that mean the automation is broken. |

`artifacts/examples/{S1,S2,N1}/verdict.json` carries the machine-readable split:
`baseline_defect_reproduced`, `harness_failures`, `blocked_steps`,
`expected_denials`, `not_applicable` and a single `harness_healthy` boolean.

### Deterministic fixture reset

A replay must not depend on what the previous run left behind, so
`POST /ops/fixtures/reset` (button on `/ops`) forgets the portal's exploration
records and restores **only** the demo chart to its documented starting state —
row limit 137, highest revenue first, `googleCategory10c` — and asserts it got
there before any scenario runs. Nothing else in Superset is touched.

```bash
# reset + replay S2/S1/N1 through the portal's own HTTP surface and export
python3 scripts/export_examples.py
```

### Safety

- Upstream Superset/MCP credentials stay server-side. The browser selects a
  *profile* (`analyst` / `restricted_viewer`); it never supplies an upstream
  identity, URL, tool name or SQL, and the portal proxies nothing arbitrary.
- Sanitization runs **before** SQLite, stdout and export: small explicit
  allowlists first, then recursive scrubbing of every surviving value and of
  free-form exception text (cookies, bearer/API/CSRF tokens, passwords,
  connection-URI credentials). Raw payloads are never stored.
  `tests/test_redaction_canary.py` plants canary secrets in headers, nested
  input, nested output and exception text and asserts they appear in none of
  the three sinks.
- All three ports bind to `127.0.0.1` only.

## Incidents

Registered semantic failures become incidents even though
`AUTO_REPAIR_ENABLED=false` — that flag disables external repair *dispatch*,
not observation. The console labels three different numbers explicitly:
**incidents**, **failed user actions** (occurrences) and **evidence events**.

| Rule | Behaviour |
| --- | --- |
| Failure family | Sibling assertions are one incident. S2's key reuse on save and the resurrected link on read are the same product defect seen twice; S1 is a separate family. The individual assertion and route survive as evidence. |
| Fingerprint | `target repo + verified baseline SHA + failure family + actor profile`. Timestamps, trace/request/event ids, exploration keys and chart ids are deliberately excluded. An environment whose running code cannot be measured fingerprints as `unverified` instead of merging with the verified baseline. |
| Delivery dedup | `event_id` is a primary key; replaying the same event is a no-op (`duplicate_event`), including across a restart and under concurrent delivery. |
| Occurrence | One per *failed user action* (`incident_id, trace_id` is a composite key), so a second assertion in the same action does not double-count, and a genuine repeat increments exactly once. |
| Never an incident | Expected N1 denials (two denial events from one attempt are still zero incidents), blocked setup, operational errors, unregistered assertions, and the portal's own harness checks. |
| Derived environments | Events from a `preview`/`reproduction`/`verification` deployment attach to the parent incident named by `PORTAL_PARENT_INCIDENT` and add evidence only — they never create an incident, so a repair session's own reproduction cannot open a second repair job. The parent scope and environment kind come from server configuration; no browser field is trusted for either. |
| State | `detected` → `candidate_fix` → `verified_in_preview`. Nothing self-promotes: a user's next attempt happening to work is not a fix. Issue, session, PR and verification fields read `not connected` until real data exists. |

### Handoff bundle

`POST /ops/incidents/{id}/export` writes four files from stored evidence only —
`incident.json`, `events.redacted.jsonl`, `reproduction.md` and `manifest.json`
(actual file list, byte sizes, SHA-256 per file, run/provenance versions,
repository and baseline SHA). `reproduction.md` carries the executable
repository/fixture setup and the exact same-tab steps, not just a localhost URL.
Events pass the Phase 2 sanitizer a second time on the way in, because the
bundle leaves the portal.

## Repair controller

An eligible incident is considered the moment it is recorded. With
`AUTO_REPAIR_ENABLED=false` the controller stops one step short of the wire:
it persists the exact issue body and the exact Devin v3 request body and shows
both in protected incident detail. Nothing is sent, and no credential is read.

| Rule | Behaviour |
| --- | --- |
| One repair at a time | A one-row `repair_slot` table is the claim. `INSERT OR IGNORE` picks the winner, so two connections, two workers or a restarted process contend in the database, not in one process's head. The claim is taken before the first remote write and spans creation, dispatch, candidate and verification. |
| Unknown outcomes | An ambiguous create, an unreadable session or a termination that may not have landed keeps the claim and leaves `needs_attention` on the record. Nothing is retried blindly and no second job can start under it. |
| Budget | ACU and wall-clock are checked before anything is sent, follow-up messages included. The deadline starts at activation, not when a disabled proposal was written. At most two follow-ups, counted durably. A candidate is never terminated before verification can answer it. |
| Candidate | GitHub, not the agent, must report the allowed repository, base `runtime-repair/baseline`, a head branch in `woohyeokk-choi/superset`, a full 40-character head SHA and an open, unmerged pull request. `candidate` is not success; only Phase 5 verification produces `verified_in_preview`. |
| Live providers | `AUTO_REPAIR_ENABLED=true` builds the real clients from `GITHUB_TOKEN`, `DEVIN_API_KEY` and `DEVIN_ORG_ID`; missing or malformed values stop start-up. There is no simulated fallback, and simulated runs use their own database and `simulated-` ids. |
| Off the request path | `RepairPoller` polls the claimed repair and settles creation claims whose worker died. Poll failures (401/403/429 included) become persisted state, never an exception in a customer request. |

### Provenance

`scripts/capture_provenance.py` measures the running container: Compose
project/service, container and image id, a hash of the Python tree **inside**
the container, the host path its mount points at and that tree's hash, the
checkout SHA and dirty flag, and the fixture row count/content revision. When
it cannot measure, the event says `unmeasured` — a host git HEAD is never
presented as proof of what is running.

## Artifacts

- `artifacts/baseline/{S1,S2,N1}/` — Phase 1 API-level reproductions (historical).
- `artifacts/examples/{S1,S2,N1}/events.redacted.jsonl` — Phase 2 portal traces
  exported from the operator viewer.
- `artifacts/examples/{S1,S2,N1}/verdict.json` — machine-readable verdict per
  scenario, separating reproduced baseline defects from harness health.
- `artifacts/examples/screenshots/` — portal and trace-viewer screenshots.
- `artifacts/phase3/examples/{S1,S2,N1}/` — Phase 3 replay (fresh run path; the
  Phase 1/2 paths above are left untouched).
- `artifacts/phase3/handoff/<fingerprint>/` — the four-file handoff bundles as
  the console wrote them.
- `artifacts/phase3/screenshots/` — incident console and browser demonstration.
- `artifacts/phase4/bootstrap/` — the clean-checkout bootstrap in an isolated
  Compose namespace: measured provenance and the S2 reproduction it produced,
  plus the MCP health caveat.

### Not covered

- Submitting the chart-sort form as the restricted profile is **not applicable**:
  the settings page is denied to that role, so the form is never rendered. The
  denial is the covered behaviour; the form is not exposed and authorization is
  not weakened to manufacture coverage.
- No public preview exists. Everything is loopback-only by design, so the URLs
  above are reachable only on the host running the stack.
- No GitHub issue, Devin session or message has been created. Every controller
  result recorded here comes from the labelled fakes in `portal/simulation.py`
  against an isolated simulation database.
- Verification does not exist yet: a repair can reach `candidate`, never
  `verified_in_preview`.
