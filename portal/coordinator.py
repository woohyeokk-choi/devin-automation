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
                       of that same directory, plus git, docker, the
                       controller credentials and the Slack webhook. It
                       claims queued proposals, dispatches them, polls them
                       and verifies candidates.

Both see one queue, because the queue is the `repairs` table and the
single-flight claim is a row in it; whichever process holds the slot holds it
against the other. That only works if the two really do share one directory,
so the portal's `/data` is a bind mount of the host's `$PORTAL_DATA_DIR`
(`stack/docker-compose.portal.yml`) rather than a named volume the host
cannot open. The coordinator's workspace, automation directory and artifacts
are host paths because the commands it runs are host commands.

All four stores are opened together by `open_state`, including the event log
the incident store reads whole traces from: an incident reconstructed without
it still lists its failing assertions but carries no `trace_events`, and the
handoff would then reach the repair session stripped of the requests and
responses around the failure.

Nothing here is given to a candidate: `IsolatedStack` builds the environment
for `docker compose` from scratch, so the coordinator's credentials stay in
the coordinator, and the Docker socket is never mounted into a candidate.

    python3 -m portal.coordinator check            # can this process run it?
    python3 -m portal.coordinator run              # the worker loop
    python3 -m portal.coordinator baseline <sha>   # prepare + validate a commit

Status notifications are published by the loop here (`portal.notify`), so the
webhook shares this process's trust boundary and no other.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import Settings, settings
from .controller import RepairStore
from .events import EventStore, utcnow
from .incidents import IncidentStore
from .isolation import IsolatedStack, replay_through_validator
from .notify import build_notifier
from .providers import Devin, GitHub
from .transport import is_simulated
from .verification import RunnerError, VerificationStore, provenance_problem
from .worker import RepairWorker, build_controller


@dataclass(frozen=True)
class State:
    """The four SQLite stores of one deployment, opened as one thing.

    `incidents.event_log` is part of the wiring and not an optional extra:
    `IncidentStore.get` returns `trace_events={}` without it, so a handoff
    built by the coordinator would lose the request/response sequence the
    portal recorded.
    """

    events: EventStore
    incidents: IncidentStore
    repairs: RepairStore
    verifications: VerificationStore


def open_state(config: Settings = settings) -> State:
    """Open the portal's state from the host side of the shared directory."""
    events = EventStore(config.db_path)
    incidents = IncidentStore(
        config.db_path.with_name("incidents.sqlite"),
        target_repo=config.target_repo,
        parent_fingerprint=config.parent_incident,
        expected_baseline=config.baseline_sha,
    )
    incidents.event_log = events
    # The portal folds events as it commits them; folding again here costs
    # nothing (an already-folded event is a duplicate by event_id) and means
    # the coordinator still sees an incident whose observer died mid-write.
    incidents.drain(events)
    return State(
        events=events,
        incidents=incidents,
        repairs=RepairStore(config.db_path.with_name("repairs.sqlite")),
        verifications=VerificationStore(
            config.db_path.with_name("verifications.sqlite")
        ),
    )


def build_worker(
    config: Settings = settings,
    *,
    state: State | None = None,
    providers: tuple[GitHub, Devin] | None = None,
) -> tuple[RepairWorker, State]:
    """The worker this module runs, assembled over the shared state.

    `providers` exists so the wiring itself can be tested without a network:
    injected clients travel the same path the live ones do, and leaving it
    unset still reads credentials from the environment rather than faking
    any.
    """
    opened = state or open_state(config)
    automation_dir = Path(config.automation_dir or Path.cwd())
    controller = build_controller(
        opened.repairs,
        target_repo=config.target_repo,
        versions={"automation_sha": _automation_ref(automation_dir)[0]},
        dispatch_enabled=config.auto_repair_enabled,
        providers=providers,
        verifications=opened.verifications,
        automation_dir=automation_dir,
        workspace=Path(config.verification_workspace or (automation_dir / "runtime")),
        artifacts=Path(config.verification_artifacts or config.data_dir / "artifacts"),
        web_port=config.verification_web_port,
        mcp_port=config.verification_mcp_port,
        incident_of=opened.incidents.get,
    )
    # Slack lives here and nowhere else: this process already holds the
    # credentials, and a webhook must never reach the portal container, a
    # repair prompt or a candidate stack. Injected providers mean the repair
    # is scripted, and a scripted repair is not allowed to post into the
    # production incident feed just because this process could — the channel
    # cannot tell the difference, so the refusal is here rather than in the
    # caller's environment.
    notifier = build_notifier(config, simulated=_is_simulated(providers))
    return RepairWorker(controller, notifier=notifier), opened


def _is_simulated(providers: tuple[GitHub, Devin] | None) -> bool:
    return providers is not None and any(
        is_simulated(provider.transport) for provider in providers
    )


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


@contextmanager
def single_instance(data_dir: Path) -> Iterator[None]:
    """One coordinator per state directory, enforced by the operating system.

    The repair slot in SQLite keeps two workers off the same *repair*, but a
    verification is not a database row: it is a checkout and a Compose
    project named after the candidate commit, and `IsolatedStack.prepare`
    removes and recreates both. Two loops over one state directory therefore
    delete each other's live candidate mid-run, which reads as a checkout or
    an init failure rather than as the deployment mistake it is. An advisory
    lock on a file in that directory is released by the kernel when the
    process dies, so a crashed coordinator does not lock its successor out.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "coordinator.lock"
    handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            held = os.read(handle, 64).decode("utf-8", "replace").strip()
            raise SystemExit(
                f"another coordinator (pid {held or 'unknown'}) owns {data_dir}"
            )
        os.truncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(handle)


def run() -> int:
    """The worker loop, in the foreground, on the host."""
    report = capability_report()
    if not report["can_verify"]:
        print(json.dumps(report, indent=2), file=sys.stderr)
        raise SystemExit("this process cannot host the runner; see the report above")
    with single_instance(settings.data_dir):
        worker, _state = build_worker(settings)
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
