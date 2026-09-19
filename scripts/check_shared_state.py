"""Prove the portal container and the host coordinator share one queue.

Run from the automation checkout with a built portal image:

    PORTAL_DATA_DIR=$PWD/runtime/state PORTAL_UID=$(id -u) PORTAL_GID=$(id -g) \\
        python3 scripts/check_shared_state.py

The portal half runs inside the container over the bind mount and the
coordinator half runs here, so the check fails on a permission or path
mistake that a same-process test would never see. Nothing here reaches the
network: the coordinator is given simulated providers, and no credential is
read.

The directory must be empty. The dispatch this performs is simulated, and a
deployment's real queue is not a place to write a simulated session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from portal.config import Settings  # noqa: E402
from portal.coordinator import build_worker  # noqa: E402
from portal.providers import Devin, GitHub  # noqa: E402
from portal.simulation import FakeDevin, FakeGitHub, FakeTransport  # noqa: E402

IMAGE = os.environ.get("PORTAL_IMAGE", "runtime-repair-portal")
CALL = "superset.save_exploration"

#: What the container runs: the portal's own observe-and-propose wiring, with
#: dispatch off, against a failure it saw.
INSIDE = """
import json
from portal.config import settings
from portal.controller import RepairStore
from portal.events import EventStore
from portal.incidents import IncidentStore
from portal.worker import build_controller

events = EventStore(settings.db_path)
incidents = IncidentStore(
    settings.db_path.with_name("incidents.sqlite"),
    target_repo=settings.target_repo,
    expected_baseline=settings.baseline_sha,
)
incidents.event_log = events
controller = build_controller(
    RepairStore(settings.db_path.with_name("repairs.sqlite")),
    target_repo=settings.target_repo,
    versions={"automation_sha": "shared-state-check"},
    dispatch_enabled=settings.auto_repair_enabled,
    incident_of=incidents.get,
)
assert settings.auto_repair_enabled is False, "the portal must not dispatch"

def observe(record):
    result = incidents.observe(record)
    found = incidents.by_fingerprint(str(result.get("fingerprint") or ""))
    if found is not None:
        controller.consider(found)

events.observer = observe
events.emit(json.loads(%(call)r))
events.emit(json.loads(%(assertion)r))
print("portal wrote", [r["state"] for r in
      RepairStore(settings.db_path.with_name("repairs.sqlite")).list()])
"""

REVISION = {
    "checkout_sha": "394bca55c792b7b3547e23f6e175a7cb0f0757e8",
    "checkout_matches_running_code": True,
    "strength": "measured",
    "fixture_revision": "sha256:67ff039835f9890c",
}
CALL_EVENT = {
    "event_id": "shared-call-1",
    "trace_id": "shared-trace",
    "step_index": 1,
    "kind": "upstream_call",
    "outcome": "ok",
    "operation": CALL,
    "actor": "analyst",
    "environment_kind": "baseline-light",
    "run_id": "shared-state-check",
    "http_status": 201,
    "input": {"row_limit": 137},
    "output": {"key": "kEy1"},
    "revision": REVISION,
}
ASSERTION_EVENT = {
    **CALL_EVENT,
    "event_id": "shared-assert-1",
    "step_index": 2,
    "kind": "assertion",
    "outcome": "assertion_failed",
    "operation": "portal.save_exploration",
    "http_status": 200,
    "scenario": "S2",
    "assertion": {
        "name": "new_exploration_does_not_reuse_a_discarded_key",
        "expected": "a fresh key",
        "observed": "the discarded key",
        "holds": False,
        "subject": "product_contract",
        "known_baseline_defect": True,
    },
}


def main() -> int:
    state = Path(os.environ["PORTAL_DATA_DIR"]).resolve()
    state.mkdir(parents=True, exist_ok=True)
    existing = sorted(p.name for p in state.glob("*.sqlite"))
    if existing:
        print(f"refusing to run against existing state {existing} in {state}")
        return 2
    user = f"{os.environ.get('PORTAL_UID', os.getuid())}:" f"{os.environ.get('PORTAL_GID', os.getgid())}"
    container = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none", "-u", user,
            "-v", f"{state}:/data",
            "-e", "PORTAL_DATA_DIR=/data",
            "-e", "AUTO_REPAIR_ENABLED=false",
            "-e", "PORTAL_TARGET_REPO=woohyeokk-choi/superset",
            IMAGE,
            "python", "-c",
            INSIDE % {
                "call": json.dumps(CALL_EVENT),
                "assertion": json.dumps(ASSERTION_EVENT),
            },
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    print(container.stdout.strip().splitlines()[-1])

    devin_api = FakeDevin()
    devin_wire = FakeTransport(devin_api)
    worker, opened = build_worker(
        Settings(
            data_dir=state,
            target_repo="woohyeokk-choi/superset",
            auto_repair_enabled=True,
            automation_dir=str(ROOT),
        ),
        providers=(
            GitHub(
                FakeTransport(FakeGitHub()),
                token="simulated-token",
                repo="woohyeokk-choi/superset",
            ),
            Devin(devin_wire, api_key="cog_simulated", org_id="org-simulated"),
        ),
    )
    actions = [d.action for d in worker.tick() if d.action != "deferred"]
    prompt = next(
        (c.json or {}).get("prompt", "") for c in devin_wire.calls if c.method == "POST"
    )
    incident = opened.incidents.get(1) or {}
    result = {
        "state_dir": str(state),
        "container_user": user,
        "portal_repairs": [r["state"] for r in opened.repairs.list()],
        "coordinator_actions": actions,
        "trace_events": {k: len(v) for k, v in incident["trace_events"].items()},
        "prompt_has_request_summary": CALL in prompt,
        "prompt_has_gap_notice": "no longer in the event log" in prompt,
        "devin_sessions": len(devin_api.sessions),
    }
    print(json.dumps(result, indent=2))
    ok = (
        actions == ["dispatched"]
        and result["trace_events"] == {"shared-trace": 2}
        and result["prompt_has_request_summary"]
        and not result["prompt_has_gap_notice"]
    )
    print("shared state check:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
