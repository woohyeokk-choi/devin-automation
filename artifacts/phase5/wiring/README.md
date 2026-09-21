# Shared state: portal container -> host coordinator

What this shows is the deployment seam, not a repair: a failure the portal
container observed becomes a proposal the trusted host coordinator claims and
dispatches, over one bind-mounted directory, with the request/response trace
still attached. No network, no credential, no GitHub issue and no Devin
session: the coordinator was given `portal.simulation` providers over a
scripted wire, and the container ran with `--network none`.

## Run

```bash
export PORTAL_DATA_DIR=$PWD/runtime/state PORTAL_UID=$(id -u) PORTAL_GID=$(id -g)
mkdir -p "$PORTAL_DATA_DIR" && chmod 0700 "$PORTAL_DATA_DIR"
set -a; . stack/.env; set +a
docker compose -f stack/docker-compose.portal.yml build portal
python3 scripts/check_shared_state.py          # writes shared-state.txt
```

`scripts/check_shared_state.py` refuses a directory that already holds state:
the dispatch it performs is simulated and does not belong in a real queue.

## Result

`shared-state.txt`, from image `sha256:12d1436…` on commit `f4e315e` plus the
wiring change under review:

| | |
|---|---|
| portal container (`AUTO_REPAIR_ENABLED=false`, uid 1000:1000) | wrote `events.sqlite`, `incidents.sqlite` and a `proposed` repair to `/data` |
| host coordinator (`build_worker`, dispatch on, simulated providers) | `dispatched` |
| `incidents.get(...)["trace_events"]` | `{"shared-trace": 2}` |
| Devin prompt | carries `superset.save_exploration` with its status and body summary, and states no missing-trace gap |

Two things this catches that an in-process test does not: the bind mount has
to be the same directory on both sides, and both processes have to be able to
*write* those SQLite files. A container running as its image's uid 10001
creates them 0644-owned by 10001, and the host coordinator's first write then
fails — hence `user: "${PORTAL_UID:-10001}:${PORTAL_GID:-10001}"` in
`stack/docker-compose.portal.yml` and the matching `PORTAL_UID`/`PORTAL_GID`
in `stack/.env.example`.

## What it is not

Simulated dispatch. Nothing here was verified, nothing reached
`verified_in_preview`, and no repaired code exists. The equivalent in-process
regressions live in `tests/test_deployment.py`.
