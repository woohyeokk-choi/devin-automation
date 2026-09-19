"""The host coordinator: the process that is actually allowed to build stacks.

Verification runs `git` and `docker compose` on the machine that owns the
Docker daemon. The portal image deliberately carries neither, and its
container is deliberately not given the Docker socket — a web application that
serves browser requests is the last place to put the ability to start
containers. So the deployment is two processes over one state directory:

    portal container   AUTO_REPAIR_ENABLED=false
                       serves the demo and the console, records events,
                       incidents and repair proposals into the bind-mounted
                       state directory, and never touches the network for
                       repair work.

    host coordinator   AUTO_REPAIR_ENABLED=true  (this module, `run`)
                       same code, same SQLite files through the *host* path
                       of that same directory, plus git, docker and the
                       controller credentials. It claims queued proposals,
                       dispatches them, polls them and verifies candidates.

Both see one queue, because the queue is the `repairs` table and the
single-flight claim is a row in it; whichever process holds the slot holds it
against the other. Paths are the only thing that differ between the two, and
they differ only by prefix: the container's `/data` and the host's
`$PORTAL_DATA_DIR` are the same directory, and the coordinator's workspace,
automation directory and artifacts are host paths because the commands it
runs are host commands.

Nothing here is given to a candidate: `IsolatedStack` builds the environment
for `docker compose` from scratch, so the coordinator's credentials stay in
the coordinator, and the Docker socket is never mounted into a candidate.

    python3 -m portal.coordinator check            # can this process run it?
    python3 -m portal.coordinator run              # the worker loop
    python3 -m portal.coordinator baseline <sha>   # prepare + validate a commit
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import settings
from .controller import RepairStore
from .events import utcnow
from .incidents import IncidentStore
from .isolation import IsolatedStack, replay_through_validator
from .verification import RunnerError, VerificationStore, provenance_problem
from .worker import RepairWorker, build_controller


def _tool(*argv: str) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".coordinator-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def capability_report() -> dict[str, Any]:
    """Whether *this* process could run a verification, and what is missing.

    Run it in the portal container and on the host: the container reports no
    git, no docker and no socket, which is the point — it is the reason the
    coordinator exists rather than an oversight to be fixed by handing the web
    application a Docker socket.
    """
    workspace = Path(settings.verification_workspace or "")
    automation = Path(settings.automation_dir or "")
    report: dict[str, Any] = {
        "checked_at": utcnow(),
        "in_container": Path("/.dockerenv").exists(),
        "git": _tool("git", "--version"),
        "docker": _tool("docker", "--version"),
        "docker_compose": _tool("docker", "compose", "version"),
        "docker_daemon": bool(_tool("docker", "info", "--format", "{{.ServerVersion}}")),
        "data_dir": str(settings.data_dir),
        "data_dir_writable": _writable(settings.data_dir),
        "automation_dir": str(automation) if settings.automation_dir else "",
        "automation_dir_present": bool(settings.automation_dir) and automation.is_dir(),
        "workspace": str(workspace) if settings.verification_workspace else "",
        "workspace_writable": bool(settings.verification_workspace)
        and _writable(workspace),
        "dispatch_enabled": settings.auto_repair_enabled,
    }
    missing = [
        name
        for name, ok in (
            ("git", bool(report["git"])),
            ("docker", bool(report["docker"])),
            ("docker compose", bool(report["docker_compose"])),
            ("a reachable docker daemon", report["docker_daemon"]),
            ("PORTAL_AUTOMATION_DIR", report["automation_dir_present"]),
            ("PORTAL_VERIFICATION_WORKSPACE", report["workspace_writable"]),
        )
        if not ok
    ]
    report["can_verify"] = not missing
    report["missing"] = missing
    return report


def _automation_ref(automation_dir: Path) -> tuple[str, bool]:
    sha = _tool("git", "-C", str(automation_dir), "rev-parse", "HEAD")
    dirty = bool(
        subprocess.run(
            ["git", "-C", str(automation_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return sha, dirty


def baseline_check(head_sha: str, cases: tuple[str, ...]) -> dict[str, Any]:
    """Prepare a stack at one commit through the live path, then grade it.

    This is the same `IsolatedStack.prepare` the verifier calls, the same
    provenance gate and the same validator; only the commit is chosen by hand.
    Pointed at the immutable baseline it is a negative control: the target
    contracts must fail and the controls must pass. It never writes to the
    baseline environment — the stack it starts is its own Compose project,
    and it is torn down afterwards.
    """
    capability = capability_report()
    if not shutil.which("docker") or not shutil.which("git"):
        raise SystemExit(f"this process cannot run a candidate stack: {capability}")
    automation_dir = Path(settings.automation_dir or Path.cwd())
    workspace = Path(settings.verification_workspace or (Path.cwd() / "runtime/candidates"))
    ref, dirty = _automation_ref(automation_dir)
    stack = IsolatedStack(
        target_repo=settings.target_repo,
        automation_dir=automation_dir,
        workspace=workspace,
        automation_ref=ref,
        web_port=settings.verification_web_port,
        mcp_port=settings.verification_mcp_port,
    )
    started = utcnow()
    environment = stack.prepare(head_sha)
    try:
        problem = provenance_problem(environment, head_sha)
        report = {} if problem else replay_through_validator(environment, cases)
    finally:
        stack.teardown(environment)
    return {
        "started_at": started,
        "finished_at": utcnow(),
        "head_sha": head_sha,
        "cases": list(cases),
        "automation_ref": ref,
        "automation_dirty": dirty,
        "provenance_problem": problem,
        "provenance": environment.provenance,
        "commands": environment.commands,
        "report": report,
        "capability": capability,
    }


def run() -> int:
    """The worker loop, in the foreground, on the host."""
    report = capability_report()
    if not report["can_verify"]:
        print(json.dumps(report, indent=2), file=sys.stderr)
        raise SystemExit("this process cannot host the runner; see the report above")
    incidents = IncidentStore(
        settings.db_path.with_name("incidents.sqlite"),
        target_repo=settings.target_repo,
        parent_fingerprint=settings.parent_incident,
        expected_baseline=settings.baseline_sha,
    )
    repairs = RepairStore(settings.db_path.with_name("repairs.sqlite"))
    verifications = VerificationStore(settings.db_path.with_name("verifications.sqlite"))
    controller = build_controller(
        repairs,
        target_repo=settings.target_repo,
        versions={"automation_sha": _automation_ref(Path(settings.automation_dir))[0]},
        dispatch_enabled=settings.auto_repair_enabled,
        verifications=verifications,
        automation_dir=Path(settings.automation_dir),
        workspace=Path(settings.verification_workspace),
        artifacts=Path(settings.verification_artifacts or settings.data_dir / "artifacts"),
        web_port=settings.verification_web_port,
        mcp_port=settings.verification_mcp_port,
        incident_of=lambda incident_id: incidents.get(incident_id),
    )
    worker = RepairWorker(controller)
    print(json.dumps({"coordinator": "running", **report}, indent=2), flush=True)
    try:
        while True:
            try:
                for decision in worker.tick():
                    print(json.dumps(asdict(decision)), flush=True)
            except Exception as exc:  # noqa: BLE001 - the loop may not die
                print(json.dumps({"tick_failed": type(exc).__name__}), flush=True)
            time.sleep(worker.interval)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run", "baseline"))
    parser.add_argument("head_sha", nargs="?", default="")
    parser.add_argument("--cases", default="S2,S1,N1")
    parser.add_argument("--out", default="", help="write the baseline result here")
    args = parser.parse_args(argv)

    if args.command == "check":
        print(json.dumps(capability_report(), indent=2))
        return 0
    if args.command == "run":
        return run()

    if len(args.head_sha) != 40:
        raise SystemExit("baseline needs the full 40-character commit id")
    cases = tuple(case.strip() for case in args.cases.split(",") if case.strip())
    try:
        result = baseline_check(args.head_sha, cases)
    except RunnerError as exc:
        print(json.dumps({"blocked": str(exc)}, indent=2))
        return 2
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    if result["provenance_problem"]:
        return 2
    return 0 if result["report"].get("verdict") == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
