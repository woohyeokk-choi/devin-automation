# devin-automation

Automation controller for the **Devin Runtime Repair** project: a synthetic-data
analytics portal backed by a fork of Apache Superset, where failing user actions
become deduplicated incidents that drive API-created Devin repair sessions and
independent verification.

Target repository: [`woohyeokk-choi/superset`](https://github.com/woohyeokk-choi/superset)
(baseline `394bca55c792b7b3547e23f6e175a7cb0f0757e8`).

The single living plan — decisions, phase status, commands and blockers — is in
[docs/EXECUTION_PLAN.md](docs/EXECUTION_PLAN.md).

**Where the code is.** Everything described here lives on the branch
`devin/1789830208-phase1-baseline-reproductions`, published as
[pull request #1](https://github.com/woohyeokk-choi/devin-automation/pull/1)
and not merged. `main` does **not** contain the application; clone the branch.

Status: two real repairs ran end to end and reached `verified_in_preview`
(Superset [#2](https://github.com/woohyeokk-choi/superset/pull/2) and
[#4](https://github.com/woohyeokk-choi/superset/pull/4), both open and
unmerged, nothing deployed). The portal never dispatches: it is fixed at
`AUTO_REPAIR_ENABLED=false` and only a trusted host coordinator holds
credentials. See [artifacts/phase6/](artifacts/phase6/) for the evidence and
[docs/results.md](docs/results.md) for the numbers.

---

## What runs here

| Piece | Where | Notes |
| --- | --- | --- |
| Superset light stack | `docker-compose-light.yml` + `stack/docker-compose.ports.yml` | Web on `127.0.0.1:8088`. The MCP sidecar is a development endpoint that authenticates as an admin user, so it is bound to `127.0.0.1:5008` and is never published. |
| Portal + operator viewer | `stack/docker-compose.portal.yml` | FastAPI, server-rendered Jinja, no frontend framework. `127.0.0.1:8090`. |
| Event store | Host directory `$PORTAL_DATA_DIR`, bind-mounted at `/data` | SQLite at `/data/events.sqlite`; the identical safe JSON is also written to the container's stdout. The host coordinator opens the same files, so this is a bind mount and not a named volume. |

The native Superset frontend is **not** built: at this revision the light stack
serves no compiled assets, and the REST/MCP paths the scenarios exercise do not
need it. The portal therefore renders real data and real chart settings itself
through the same REST/MCP clients the scenarios use — nothing is mocked.

## Start it

Two checkouts are needed — this repository and the Superset fork at the
baseline revision:

```bash
git clone --branch devin/1789830208-phase1-baseline-reproductions \
  https://github.com/woohyeokk-choi/devin-automation.git
git clone https://github.com/woohyeokk-choi/superset.git
git -C superset checkout 394bca55c792b7b3547e23f6e175a7cb0f0757e8
```

Requires Docker with the Compose plugin and Python 3.11 on the host. Nothing
below needs a credential; the only secrets in the whole project belong to the
coordinator, and only when live dispatch is enabled.

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

# 4. the shared state directory must exist, be owned by you and be exported
#    BEFORE Compose starts: the compose file requires PORTAL_DATA_DIR and the
#    container runs as PORTAL_UID:PORTAL_GID, so a directory created later (or
#    created by the container as uid 10001) leaves one of the two processes
#    unable to write the SQLite files.
export PORTAL_DATA_DIR=$PWD/runtime/state
export PORTAL_UID=$(id -u) PORTAL_GID=$(id -g)
mkdir -p "$PORTAL_DATA_DIR" && chmod 0700 "$PORTAL_DATA_DIR"

# 5. portal + operator viewer
docker compose -f stack/docker-compose.portal.yml up -d --build
curl -s http://127.0.0.1:8090/healthz
```

All three ports bind to `127.0.0.1`; nothing is published. To watch it from
another machine, forward the port over SSH rather than changing the bind.

### Where things are written

| What | Where |
| --- | --- |
| Structured events | `$PORTAL_DATA_DIR/events.sqlite`, and the same safe JSON on the container's stdout (`docker compose -f stack/docker-compose.portal.yml logs -f portal`) |
| Incidents, repairs, verifications | `$PORTAL_DATA_DIR/{incidents,repairs,verifications}.sqlite` |
| Verification reports | `$PORTAL_DATA_DIR/artifacts/repair-<id>/<sha>-<timestamp>.json` |
| Coordinator lock | `$PORTAL_DATA_DIR/coordinator.lock` |
| Handoff bundles | `$PORTAL_DATA_DIR/handoff/<fingerprint>/`, downloadable from the console |
| Operator UI | `/ops` (events), `/ops/incidents` (incidents, repairs, verification attempts), `/ops/export.jsonl` (redacted export) |
| Candidate checkouts | `$PORTAL_VERIFICATION_WORKSPACE` (default `runtime/candidates`), removed after each attempt |
| Published evidence | `artifacts/` in this repository |

### Known setup pitfalls

- `PORTAL_DATA_DIR` is required by the compose file (`${PORTAL_DATA_DIR:?}`) —
  Compose fails fast rather than silently creating a named volume the host
  cannot read.
- If `PORTAL_UID`/`PORTAL_GID` do not match the owner of that directory, the
  first cross-process write fails with a permission error, not a data error.
- Compose names a built image after its project, so in an isolated namespace
  set `COMPOSE_PROJECT_NAME` *and* `SUPERSET_LIGHT_IMAGE=<project>-superset-light`.
- The MCP sidecar is ready when the MCP protocol answers (`initialize`, then
  `tools/list`); the web container's `/health` says nothing about it.
- Verification needs free ports: with the demo stack up, pass
  `PORTAL_VERIFICATION_WEB_PORT` / `PORTAL_VERIFICATION_MCP_PORT`.
- Container-created bytecode under a candidate checkout is root-owned;
  teardown handles it, but a manual `rm -rf` may need `sudo`.

### Cleanup

Scoped to this project only — never `docker system prune`:

```bash
docker compose -f stack/docker-compose.portal.yml down            # portal
cd "$SUPERSET_DIR" && docker compose -f docker-compose-light.yml \
  -f "$AUTOMATION_DIR/stack/docker-compose.ports.yml" down        # baseline
docker compose -p candidate<sha12> down -v                        # a candidate
```

The verifier removes its own candidate project, checkout and volumes after
every attempt; the command above is for one it did not get to. Leave
`$PORTAL_DATA_DIR` alone unless you mean to discard the recorded history.

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
- Secrets are passed as environment variables to the process that needs them
  and to nothing else: demo/operator passwords and the upstream Superset
  identity to the portal, `DEVIN_API_KEY`/`DEVIN_ORG_ID` to the coordinator.
  Nothing is written to the repository, the state directory or an artifact,
  and no credential is ever given to a candidate stack or a repair session.
  GitHub needs **no broad personal access token**: the host adapter resolves a
  fresh credential per request from the existing `gh` authentication, so a
  two-hour repair outlives a one-hour installation token. `GITHUB_TOKEN` is
  still honoured where a deployment outside this machine sets one.

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
| Off the request path | A request persists the event, admits the incident and writes a proposal — nothing more. `RepairWorker` is the only place repair work touches the network: it settles creation claims whose worker died, walks the repair holding the slot (dispatch, poll or verify) and claims the oldest queued proposal once the slot frees, so a second incident starts without another browser action and a customer never waits on api.github.com. Poll failures (401/403/429 included) become persisted state, never an exception in a customer request. |

## Independent verification

`candidate` means the agent opened a pull request. It is not success. What
decides is `portal/verification.py`, built so the session under test cannot
influence the answer.

| Stage | Rule |
| --- | --- |
| Candidate | Re-read from GitHub: allowed host and repository, head branch inside `woohyeokk-choi/superset`, base `runtime-repair/baseline`, open, unmerged, full 40-character head SHA. |
| Scope | Changed paths are judged before anything is built. Automation, validator, fixtures, auth/CSRF, workflows, dependencies and Docker bootstrap are forbidden. A scope rejection is `blocked` — no candidate code ran, so there is nothing to feed back as a product failure. |
| Isolation | The exact head SHA is checked out on its own and brought up as its own Compose project, ports, database, cache, volumes and network. No Docker socket, no controller/GitHub/Devin credential in the environment or the mounts, and the stack refuses to start if a known secret name is present. |
| Assertions | `portal/validator.py` at a pinned automation revision in the trusted checkout — never the candidate's copy. |
| Verdict | `passed` only when every registered target and control check ran and held. Setup failure, empty traces, skipped checks, missing assertions, transport errors, a moved head or provenance that disagrees with the SHA under test are `blocked`, never a pass and never a product failure. The head is re-read before a verdict is accepted. |
| Outcome | A pass is `verified_in_preview`. Not deployed, not merged, not production-ready. Simulated attempts are stored separately and excluded from the verified count. |
| Feedback | Failures return the precise expected/observed lines to the same session, once per candidate SHA, at most twice, and only after the ACU and deadline checks. |

### Who runs it

The portal container deliberately cannot verify: it has no git, no Docker CLI
and no repair credential, and it only observes events, records incidents and
writes proposals. A trusted **host coordinator** owns git, Docker, the
candidate workspace and the controller credentials, and drains the same
durable SQLite state the portal writes to. Candidate containers get neither
the Docker socket nor any credential.

```bash
python3 -m portal.coordinator check          # what this process can do
python3 -m portal.coordinator baseline <superset-sha> --out result.json
python3 -m portal.coordinator run            # drain proposals, poll, verify
```

Exactly one coordinator may own a state directory. `run` takes an advisory
lock on `$PORTAL_DATA_DIR/coordinator.lock` and exits if another process
holds it: the repair slot in SQLite keeps two workers off one repair, but a
verification is a checkout and a Compose project named after the candidate
commit, and a second loop would delete and recreate the first one's candidate
mid-run — which surfaces as a checkout or init failure rather than as the
deployment mistake it is. The kernel releases the lock when the process dies.

Two things have to exist on the target fork before dispatch is enabled, and
the controller creates neither at run time: **Issues** enabled, and a
`runtime-repair` label for the issues it opens or reuses. Both are in place on
`woohyeokk-choi/superset`; on another fork, create them first.

#### The shared state directory

"The same durable SQLite state" is one host directory, and the two processes
have to agree on it:

```bash
export PORTAL_DATA_DIR=$PWD/runtime/state     # /data inside the container
export PORTAL_UID=$(id -u) PORTAL_GID=$(id -g)
mkdir -p "$PORTAL_DATA_DIR" && chmod 0700 "$PORTAL_DATA_DIR"

set -a; . stack/.env; set +a
docker compose -f stack/docker-compose.portal.yml up -d --build   # observes
GITHUB_TOKEN=... DEVIN_API_KEY=... AUTO_REPAIR_ENABLED=true \
  PORTAL_DATA_DIR=$PORTAL_DATA_DIR python3 -m portal.coordinator run
```

Both processes *write* those files, so the container runs as the coordinator's
uid rather than the image's own 10001: files created at the default 0644 by
one uid are read-only to the other, and the coordinator's first write fails.
The portal is fixed at `AUTO_REPAIR_ENABLED=false` in the Compose file — the
credentials, the git and Docker access and the dispatching all belong to the
coordinator, and the Docker socket is mounted nowhere.

#### Try the whole seam without a credential

One command, no network, no account, nothing live — the real portal image
under `--network none` plus the coordinator's own wiring against
`FakeGitHub`/`FakeDevin`:

```bash
docker build -t runtime-repair-portal .
PORTAL_DATA_DIR=$PWD/runtime/sim PORTAL_UID=$(id -u) PORTAL_GID=$(id -g) \
  python3 scripts/check_shared_state.py     # prints "shared state check: PASS"
```

Everything it reports is **simulated**: the session id is `simulated-…`, the
issue and pull request come from the fakes, and `FakeTransport` is the only
transport involved, so no request leaves the machine. It refuses to run
against a state directory that already holds SQLite files, which is what keeps
a simulated dispatch out of a live queue — point it at a throwaway directory,
never at the live state. `artifacts/phase7/simulation/` is a recorded run of
exactly this command from a clean clone.

It proves the seam end to end without a network: the real portal image (`--network none`) records a failure and a
proposal in the bind mount, and the coordinator's own wiring claims it and
dispatches it to simulated providers with the request trace intact —
`artifacts/phase5/wiring/`.

`baseline` is the live verification path with the baseline commit in place of
a candidate: it calls `IsolatedStack.prepare()`, checks provenance, replays
the validator through the prepared stack and tears the stack down. Point it at
free ports if the demo stack is up:

```bash
PORTAL_VERIFICATION_WEB_PORT=8388 PORTAL_VERIFICATION_MCP_PORT=5308 \
PORTAL_VERIFICATION_WORKSPACE=/tmp/candidates \
python3 -m portal.coordinator baseline 394bca55c792b7b3547e23f6e175a7cb0f0757e8 \
  --out baseline-result.json   # exit 0 passed, 1 product failure, 2 blocked
```

The validator also runs standalone, which is how it is proved to catch the
defects it claims to. **Two** defects are reproduced here — S2's discarded
form-data key and S1's row-limit reset — and they fail **three** target
assertions, because S2 is visible both on save and on the stale link:

```bash
python3 -m portal.validator --case S2 --case S1 --case N1 \
  --base-url http://127.0.0.1:8088 --mcp-url http://127.0.0.1:5008/mcp \
  --out report.json     # exit 0 passed, 1 product-contract failed, 2 blocked
```

S1 runs only after MCP readiness is established in the MCP protocol itself
(`initialize`, then `tools/list`) — the web container answering `/health` says
nothing about a different process in a different container.

### Provenance

`scripts/capture_provenance.py` measures the running container: Compose
project/service, container and image id, a hash of the Python tree **inside**
the container, the host path its mount points at and that tree's hash, the
checkout SHA and dirty flag, and the fixture content revision. When
it cannot measure, the event says `unmeasured` — a host git HEAD is never
presented as proof of what is running.

The fixture revision digests the region/channel/product revenue aggregate the
chart-data API returns, so its `rows` count is that grouped projection (60),
not the 600 seeded records. It catches fixture content that would move a
scenario's numbers rather than every possible row-level difference.

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
- `artifacts/phase4/bootstrap/` — bootstrap in an isolated Compose namespace:
  measured provenance and the S2 reproduction it produced. Its README carries
  a correction: that checkout was dirty, so it was not a published-code run.
- `artifacts/phase5/bootstrap/` — the published-code bootstrap: clean clones of
  both repositories, isolated project `phase5check`, a healthy MCP container
  under a protocol-level healthcheck, and the discovered MCP tool list.
- `artifacts/phase5/negative-control/` — the real validator against the
  immutable baseline: S2 and S1 fail their target contracts, every control and
  N1 pass, and three unreachable/unauthenticated variants block.
- `artifacts/phase5/integrated/` — the same negative control, but produced by
  the code path live verification uses: the coordinator called
  `IsolatedStack.prepare()`, which built its own candidate stack
  (`candidate394bca55c792`, ports 8388/5308), measured provenance and ran the
  pinned validator through it, then removed everything it created.

- `artifacts/phase6/S2/`, `artifacts/phase6/S1/` — the two live repairs: the
  full verification record (every check with expected and observed values),
  the attempts, and a README stating exactly what was and was not run.
- `artifacts/phase7/simulation/` — the credential-free simulation above, run
  from a clean clone of the published branch.

### Not covered

- Submitting the chart-sort form as the restricted profile is **not applicable**:
  the settings page is denied to that role, so the form is never rendered. The
  denial is the covered behaviour; the form is not exposed and authorization is
  not weakened to manufacture coverage.
- No public preview exists. Everything is loopback-only by design, so the URLs
  above are reachable only on the host running the stack.
- Verification runs from the trusted host coordinator, never from inside the
  portal container, which has no git or Docker CLI and reports
  `can_verify=false`.
- **No CI ran on either candidate.** Both head SHAs have 0 check-runs and 0
  commit statuses; the combined `pending` is the absence of reporting, not a
  passing build. The only independent checks are this validator's behavioural
  assertions.
- **Nothing is merged or deployed.** `verified_in_preview` is the end state,
  and because neither product pull request is merged, the baseline is still
  buggy.
- **No Slack integration.** A channel, app and bot exist, but this repository
  contains no outbound notifier, and native session sync is available only to
  a session's owner — the `superset-runtime-repair` service user. No alerting
  or Slack Q&A works today.
- Human touch time and cost per repair were not measured, and the API's
  `acus_consumed: 0.0` is reported as received, not as a claim that the work
  was free.
