"""Live provider construction and the background repair poller.

Two things kept out of the request path: building the real GitHub and Devin
clients from configuration (never from a fake, and never silently), and
walking the one in-flight repair on a timer so no customer request ever waits
on api.github.com.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from .controller import Controller, Decision, RepairStore
from .providers import Devin, GitHub, NotConfigured
from .transport import HttpTransport, Transport

log = logging.getLogger("portal.worker")


def live_providers(
    target_repo: str, transport: Transport | None = None
) -> tuple[GitHub, Devin]:
    """The real clients, or a clear refusal.

    `NotConfigured` propagates: a deployment that turns dispatch on without
    credentials must fail to start. Falling back to a simulated provider here
    would turn "nothing was configured" into "the repair succeeded".
    """
    wire = transport or HttpTransport()
    github = GitHub(
        transport=wire, token=os.environ.get("GITHUB_TOKEN", ""), repo=target_repo
    )
    devin = Devin(
        transport=wire,
        api_key=os.environ.get("DEVIN_API_KEY", ""),
        org_id=os.environ.get("DEVIN_ORG_ID", ""),
    )
    return github, devin


def build_controller(
    store: RepairStore,
    *,
    target_repo: str,
    versions: dict[str, Any],
    dispatch_enabled: bool,
    providers: tuple[GitHub, Devin] | None = None,
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
    return Controller(
        store,
        target_repo=target_repo,
        versions=versions,
        github=github,
        devin=devin,
        dispatch_enabled=True,
        **kwargs,
    )


class RepairPoller:
    """Walks the claimed repair on a timer, bounded and off the request path.

    One tick: resolve creation claims whose creator died, then poll whichever
    repair holds the single-flight slot. Every failure the controller records
    is persisted state; this loop only decides when to look.
    """

    def __init__(
        self,
        controller: Controller,
        *,
        interval_seconds: float = 30.0,
        stale_after_minutes: int = 15,
    ) -> None:
        self.controller = controller
        self.interval = interval_seconds
        self.stale_after_minutes = stale_after_minutes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> list[Decision]:
        decisions: list[Decision] = list(
            self.controller.recover(self.stale_after_minutes)
        )
        active = self.controller.store.active()
        if active is not None and active["session_id"]:
            decisions.append(self.controller.poll(int(active["id"])))
        return decisions

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a worker thread may not die
                log.exception("repair poll tick failed")

    def start(self) -> None:
        if self._thread is not None or not self.controller.dispatch_enabled:
            # Nothing to poll while dispatch is off: no session exists.
            return
        self._thread = threading.Thread(
            target=self._run, name="repair-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
            self._thread = None


__all__ = [
    "NotConfigured",
    "RepairPoller",
    "build_controller",
    "live_providers",
]
