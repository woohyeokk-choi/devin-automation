"""Live provider construction and the background repair poller.

Two things kept out of the request path: building the real GitHub and Devin
clients from configuration (never from a fake, and never silently), and
walking the one in-flight repair on a timer so no customer request ever waits
on api.github.com.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from .brief import BASE_BRANCH
from .controller import Controller, Decision, RepairStore
from .isolation import IsolatedStack, replay_through_validator
from .notify import Notifier, message_for
from .providers import Devin, GitHub, NotConfigured
from .transport import HttpTransport, Transport
from .verification import VerificationStore, Verifier

log = logging.getLogger("portal.worker")


def gh_credential() -> str:
    """The host's gh-managed token, read again for every request.

    A GitHub App installation token expires about an hour after it is issued,
    which is shorter than one repair's deadline, so the worker asks `gh` each
    time instead of capturing one at startup. The value is returned to the
    caller's header and nowhere else: not logged, not stored, not passed to a
    candidate stack.
    """
    try:
        done = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise NotConfigured(f"gh credential unavailable ({type(error).__name__})")
    if done.returncode != 0:
        raise NotConfigured("gh is not authenticated")
    return done.stdout.strip()


def github_credential() -> str | Callable[[], str]:
    """`GITHUB_TOKEN` where a deployment sets one, the host's gh otherwise."""
    return os.environ.get("GITHUB_TOKEN") or gh_credential


def live_providers(
    target_repo: str, transport: Transport | None = None
) -> tuple[GitHub, Devin]:
    """The real clients, or a clear refusal.

    `NotConfigured` propagates: a deployment that turns dispatch on without
    credentials must fail to start. Falling back to a simulated provider here
    would turn "nothing was configured" into "the repair succeeded".
    """
    wire = transport or HttpTransport()
    github = GitHub(transport=wire, token=github_credential(), repo=target_repo)
    devin = Devin(
        transport=wire,
        api_key=os.environ.get("DEVIN_API_KEY", ""),
        org_id=os.environ.get("DEVIN_ORG_ID", ""),
    )
    return github, devin


def live_verifier(
    github: GitHub,
    verifications: VerificationStore,
    *,
    target_repo: str,
    automation_dir: Path,
    workspace: Path,
    automation_ref: str,
    artifacts: Path,
    automation_dirty: bool = False,
    web_port: int = 8288,
    mcp_port: int = 5208,
) -> Verifier:
    """The real verifier: an isolated candidate stack graded by the pinned validator.

    `automation_ref` is the revision the assertions come from. It is the
    deployment's, not the candidate's: the repair session can change product
    code, and nothing else that takes part in judging it. It has to be a
    commit the candidate checkout can fetch, so a dirty deployment tree is
    recorded next to the ref rather than folded into it — uncommitted work
    is not in the revision the verifier actually runs.
    """
    return Verifier(
        github=github,
        runner=IsolatedStack(
            target_repo=target_repo,
            automation_dir=automation_dir,
            workspace=workspace,
            automation_ref=automation_ref,
            web_port=web_port,
            mcp_port=mcp_port,
        ),
        store=verifications,
        target_repo=target_repo,
        base_branch=BASE_BRANCH,
        validator_ref=automation_ref + (" (deployment tree dirty)" if automation_dirty else ""),
        replay=replay_through_validator,
        artifacts=artifacts,
        simulated=False,
    )


def build_controller(
    store: RepairStore,
    *,
    target_repo: str,
    versions: dict[str, Any],
    dispatch_enabled: bool,
    providers: tuple[GitHub, Devin] | None = None,
    verifications: VerificationStore | None = None,
    automation_dir: Path | None = None,
    workspace: Path | None = None,
    artifacts: Path | None = None,
    web_port: int = 8288,
    mcp_port: int = 5208,
    **kwargs: Any,
) -> Controller:
    """A controller wired for the mode it is actually in.

    With dispatch off there are no providers and no credentials are read: the
    controller records the exact bodies it would send. With dispatch on the
    live clients are constructed, and missing configuration raises.
    """
    if not dispatch_enabled:
        return Controller(
            store,
            target_repo=target_repo,
            versions=versions,
            dispatch_enabled=False,
            **kwargs,
        )
    github, devin = providers or live_providers(target_repo)
    verifier = None
    if verifications is not None and automation_dir and workspace and artifacts:
        verifier = live_verifier(
            github,
            verifications,
            target_repo=target_repo,
            automation_dir=automation_dir,
            workspace=workspace,
            automation_ref=str(versions.get("automation_sha") or ""),
            automation_dirty=bool(versions.get("automation_dirty")),
            artifacts=artifacts,
            web_port=web_port,
            mcp_port=mcp_port,
        )
    return Controller(
        store,
        target_repo=target_repo,
        versions=versions,
        github=github,
        devin=devin,
        dispatch_enabled=True,
        verifier=verifier,
        **kwargs,
    )


class RepairWorker:
    """The only place repair work touches the network.

    One tick asks the controller to advance the world by one step: settle
    creation claims whose creator died, walk the repair holding the
    single-flight slot — dispatch, poll or verify it — and, when the slot is
    free, claim the oldest queued proposal. So a second incident raised while
    the first is in flight starts on its own, with no further browser action,
    and a customer request never waits on api.github.com.

    The queue is the `repairs` table. There is no broker to lose it, and a
    restart resumes from whatever the database says.
    """

    def __init__(
        self,
        controller: Controller,
        *,
        interval_seconds: float = 30.0,
        stale_after_minutes: int = 15,
        notifier: Notifier | None = None,
    ) -> None:
        self.controller = controller
        self.interval = interval_seconds
        self.stale_after_minutes = stale_after_minutes
        controller.stale_after_minutes = stale_after_minutes
        self.notifier = notifier
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> list[Decision]:
        decisions = self.controller.advance()
        self._announce(decisions)
        return decisions

    def _announce(self, decisions: list[Decision]) -> None:
        """Tell the channel what already happened.

        After the controller has written its decision, never during it: a
        notifier that is missing, misconfigured or failing must leave the
        repair exactly as it was, so every failure here is swallowed into the
        delivery ledger and the log.
        """
        if self.notifier is None:
            return
        try:
            for decision in decisions:
                if decision.repair_id is None:
                    continue
                repair = self.controller.store.get(decision.repair_id)
                if repair is None:
                    continue
                message = message_for(
                    decision.action,
                    repair,
                    self.controller.incident_of(int(repair["incident_id"])),
                )
                if message is not None:
                    self.notifier.publish(*message, repair_id=decision.repair_id)
            self.notifier.deliver_due()
        except Exception:  # noqa: BLE001 - a status message may not break a repair
            log.exception("status notification failed")

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a worker thread may not die
                log.exception("repair worker tick failed")

    def start(self) -> None:
        if self._thread is not None or not self.controller.dispatch_enabled:
            # Nothing to poll while dispatch is off: no session exists.
            return
        self._thread = threading.Thread(
            target=self._run, name="repair-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
            self._thread = None


#: The poller became a worker when dispatch moved off the request path; the
#: old name still resolves so existing deployments and tests keep working.
RepairPoller = RepairWorker

__all__ = [
    "NotConfigured",
    "live_verifier",
    "RepairPoller",
    "RepairWorker",
    "build_controller",
    "live_providers",
]
