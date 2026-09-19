# Credential-free simulation, from a clean clone

`run.txt` is the recorded output of the one command anyone can run to see the
whole seam work without an account, a credential or a network:

```bash
git clone --branch devin/1789830208-phase1-baseline-reproductions \
  https://github.com/woohyeokk-choi/devin-automation.git && cd devin-automation
docker build -t runtime-repair-portal .
PORTAL_DATA_DIR=$PWD/runtime/sim PORTAL_UID=$(id -u) PORTAL_GID=$(id -g) \
  python3 scripts/check_shared_state.py
```

Recorded 2026-09-19 from clone `79c9c94ab6b0df213b57612928cf32ffc930f20f`
(clean working tree) and image
`sha256:64da1872be7d6d2fab0f561f19aa0a2bd9e6fa9ed1c6de159a527dce8b7835ad`,
built by that clone's own `Dockerfile`.

**Everything it produces is SIMULATED.** The Devin session, the issue and the
pull request come from `FakeDevin`/`FakeGitHub` over `FakeTransport`
(`portal/simulation.py`); ids are prefixed `simulated-`. No real API is
reached, nothing is billed, and no result here is evidence about the product.
The real runs are in `artifacts/phase6/`.

What it nevertheless exercises for real:

- the **real portal image**, run with `--network none` and the state directory
  bind-mounted, writing a failure and a `proposed` repair with
  `AUTO_REPAIR_ENABLED=false` asserted inside the container
- the **coordinator's production wiring** (`build_worker`) on the host,
  claiming that proposal out of the shared SQLite files and dispatching it
- the handoff: `trace_events {"shared-trace": 2}` and the prompt carrying the
  recorded `superset.save_exploration` request, with no missing-trace notice

Two isolation properties are visible in the transcript. The container runs
with no network at all, so the portal half cannot reach anything even if it
tried; and the script **refuses a state directory that already contains
SQLite files** (exit 2, shown at the end of `run.txt` against the live state
directory), which is what keeps a simulated dispatch out of a real queue.
Point it at a throwaway directory.
