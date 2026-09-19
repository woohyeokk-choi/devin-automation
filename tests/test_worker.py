"""Provider wiring and the background poller, with no live call anywhere.

The fakes are injected explicitly. Missing credentials must be an error the
deployment sees at start-up, never a quiet fall back to a simulated success.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from portal.controller import NEEDS_ATTENTION, Controller, RepairStore
from portal.incidents import IncidentStore
from portal.providers import Devin, GitHub, NotConfigured
from portal.simulation import FakeDevin, FakeGitHub, FakeTransport
from portal.transport import Response
from portal.worker import (
    RepairPoller,
    RepairWorker,
    build_controller,
    gh_credential,
    github_credential,
    live_providers,
)

from test_incidents import REPO, event

VERSIONS = {"automation_sha": "abc123", "automation_dirty": False}


@pytest.fixture()
def incident(tmp_path: Path) -> dict[str, Any]:
    store = IncidentStore(tmp_path / "incidents.sqlite", target_repo=REPO)
    store.observe(event(event_id="w1"))
    found = store.get(1)
    assert found is not None
    return found


@pytest.fixture()
def repairs(tmp_path: Path) -> RepairStore:
    return RepairStore(tmp_path / "simulated-repairs.sqlite", simulated=True)


@pytest.fixture()
def live_repairs(tmp_path: Path) -> RepairStore:
    """Rows that do not declare themselves scripted.

    Delivery reads that flag off the record, so a test about what reaches
    the channel has to own rows a real run would have written.
    """
    return RepairStore(tmp_path / "repairs.sqlite")


def fakes() -> tuple[GitHub, Devin, FakeGitHub, FakeDevin]:
    github_api, devin_api = FakeGitHub(), FakeDevin()
    return (
        GitHub(FakeTransport(github_api), token="simulated-token", repo=REPO),
        Devin(FakeTransport(devin_api), api_key="cog_simulated", org_id="org-simulated"),
        github_api,
        devin_api,
    )


def test_a_disabled_deployment_needs_no_credentials(repairs: RepairStore) -> None:
    controller = build_controller(
        repairs, target_repo=REPO, versions=VERSIONS, dispatch_enabled=False
    )
    assert controller.github is None and controller.devin is None


def test_enabling_dispatch_without_credentials_is_an_error(
    repairs: RepairStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("GITHUB_TOKEN", "DEVIN_API_KEY", "DEVIN_ORG_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(NotConfigured):
        build_controller(
            repairs, target_repo=REPO, versions=VERSIONS, dispatch_enabled=True
        )


def test_a_browser_slug_is_not_a_v3_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_simulated")
    monkeypatch.setenv("DEVIN_API_KEY", "some-session-slug")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-simulated")
    with pytest.raises(NotConfigured):
        live_providers(REPO)


@dataclass
class RecordingWire:
    """A wire that remembers what each request was authenticated with."""

    seen: list[str] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Response:
        self.seen.append(headers["Authorization"])
        return Response(status=200, body={"head": {}, "base": {}})


def test_each_request_asks_the_credential_source_again() -> None:
    """A token issued at start-up expires before a two-hour repair ends."""
    issued = iter(["first-token", "second-token"])
    wire = RecordingWire()
    github = GitHub(transport=wire, token=lambda: next(issued), repo=REPO)

    github.pull_request_head(1)
    github.pull_request_head(1)

    assert wire.seen == ["Bearer first-token", "Bearer second-token"]


def test_an_unavailable_credential_refuses_instead_of_calling_anonymously() -> None:
    """An unauthenticated read answers 'no such issue' and opens a second one."""
    wire = RecordingWire()
    github = GitHub(transport=wire, token=lambda: "", repo=REPO)

    with pytest.raises(NotConfigured):
        github.pull_request_head(1)

    assert wire.seen == []


def test_an_explicit_token_is_preferred_over_the_hosts_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_deployment")
    assert github_credential() == "ghp_deployment"

    monkeypatch.delenv("GITHUB_TOKEN")
    assert github_credential() is gh_credential


def test_an_unauthenticated_gh_is_not_a_blank_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "portal.worker.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "not logged in"),
    )
    with pytest.raises(NotConfigured):
        gh_credential()


def worker_controller(
    repairs: RepairStore, incidents: dict[int, dict[str, Any]], providers: Any
) -> Controller:
    return build_controller(
        repairs,
        target_repo=REPO,
        versions=VERSIONS,
        dispatch_enabled=True,
        providers=providers,
        incident_of=incidents.get,
    )


def test_a_request_only_queues_and_the_worker_dispatches(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    github, devin, github_api, devin_api = fakes()
    controller = worker_controller(repairs, {1: incident}, (github, devin))

    # What the HTTP handler does. Nothing is sent.
    assert controller.consider(incident).action == "queued"
    assert not devin_api.sessions and not github_api.issues

    # What the worker does, on its own timer.
    assert [d.action for d in RepairWorker(controller).tick()] == ["dispatched"]
    assert len(devin_api.sessions) == 1


def test_a_customer_request_does_not_wait_for_github_or_devin(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    """Slow remotes belong to the worker's clock, not the customer's."""
    github_api, devin_api = FakeGitHub(), FakeDevin()

    def slowly(handler: Any) -> Any:
        def wrapped(call: Any) -> Any:
            time.sleep(0.5)
            return handler(call)

        return wrapped

    github = GitHub(
        FakeTransport(slowly(github_api)), token="simulated-token", repo=REPO
    )
    devin = Devin(
        FakeTransport(slowly(devin_api)), api_key="cog_simulated", org_id="org-simulated"
    )
    controller = worker_controller(repairs, {1: incident}, (github, devin))

    start = time.monotonic()
    controller.consider(incident)
    request_seconds = time.monotonic() - start

    start = time.monotonic()
    RepairWorker(controller).tick()
    worker_seconds = time.monotonic() - start

    assert request_seconds < 0.1, request_seconds
    assert worker_seconds >= 0.5, worker_seconds
    assert len(devin_api.sessions) == 1


def test_the_next_queued_incident_starts_without_another_browser_action(
    repairs: RepairStore, incident: dict[str, Any], tmp_path: Path
) -> None:
    other_store = IncidentStore(tmp_path / "second.sqlite", target_repo=REPO)
    other_store.observe(
        event(
            event_id="w2",
            assertion="row_limit_survives_an_unrelated_change",
            trace_id="tw2",
        )
    )
    second = other_store.get(1)
    assert second is not None
    second = dict(second, id=2)

    github, devin, _, devin_api = fakes()
    controller = worker_controller(repairs, {1: incident, 2: second}, (github, devin))
    controller.consider(incident)
    controller.consider(second)

    worker = RepairWorker(controller)
    worker.tick()  # the first one claims the slot
    assert len(devin_api.sessions) == 1
    first_id = repairs.slot_holder()

    # While the first is in flight the second waits, whatever the worker does.
    worker.tick()
    assert len(devin_api.sessions) == 1

    # The first one finishes and releases; no request, no console, no button.
    assert first_id is not None
    repairs.release_slot(int(first_id))
    repairs.update(int(first_id), state="terminal", terminal_reason="simulated finish")
    worker.tick()
    assert len(devin_api.sessions) == 2


def test_the_queue_survives_a_restart(
    repairs: RepairStore, incident: dict[str, Any], tmp_path: Path
) -> None:
    """A proposal written before a crash is dispatched by the next process."""
    disabled = build_controller(
        repairs, target_repo=REPO, versions=VERSIONS, dispatch_enabled=False
    )
    disabled.consider(incident)
    repairs.close()

    reopened = RepairStore(tmp_path / "simulated-repairs.sqlite", simulated=True)
    github, devin, _, devin_api = fakes()
    controller = worker_controller(reopened, {1: incident}, (github, devin))
    assert [d.action for d in RepairWorker(controller).tick()] == ["dispatched"]
    assert len(devin_api.sessions) == 1


def test_the_worker_records_a_rejected_poll_instead_of_raising(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    github, devin, _, devin_api = fakes()
    controller = worker_controller(repairs, {1: incident}, (github, devin))
    controller.consider(incident)
    RepairWorker(controller).tick()

    for status in (401, 403, 429):
        devin_api.status = status
        # An operator re-opening the parked repair for the next answer.
        holder = repairs.slot_holder()
        assert holder is not None
        repairs.update(int(holder), state="dispatched", attention=None)
        decisions = RepairWorker(controller).tick()
        assert [d.action for d in decisions] == ["parked"]
        repair = repairs.active()
        assert repair is not None and repair["state"] == NEEDS_ATTENTION
        assert str(status) in str(repair["attention"])
        # The claim is not released: that session is still out there.
        assert repairs.slot_holder() == int(repair["id"])


def test_the_poller_resolves_a_creation_claim_whose_worker_died(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    github, devin, _, _ = fakes()
    clock = datetime.now(timezone.utc)
    controller = Controller(
        repairs,
        target_repo=REPO,
        versions=VERSIONS,
        github=github,
        devin=devin,
        dispatch_enabled=True,
        now=lambda: clock + timedelta(hours=1),
    )
    repair = controller._propose(incident)
    controller.store.intend(int(repair["id"]), "issue", "marker-1")

    decisions = RepairPoller(controller).tick()

    assert [d.action for d in decisions] == ["parked"]
    stalled = repairs.get(int(repair["id"]))
    assert stalled is not None and stalled["state"] == NEEDS_ATTENTION


def test_the_poller_stays_asleep_while_dispatch_is_disabled(
    repairs: RepairStore,
) -> None:
    controller = build_controller(
        repairs, target_repo=REPO, versions=VERSIONS, dispatch_enabled=False
    )
    poller = RepairPoller(controller, interval_seconds=0.01)
    poller.start()
    try:
        assert poller._thread is None
    finally:
        poller.stop()


# --- status notifications --------------------------------------------------


def test_the_worker_announces_a_dispatch_and_says_nothing_about_polling(
    live_repairs: RepairStore, incident: dict[str, Any], tmp_path: Path
) -> None:
    from portal.notify import NotificationLog, Notifier

    from test_notify import WEBHOOK, Recorder

    wire = Recorder()
    notifier = Notifier(
        log=NotificationLog(tmp_path / "notifications.sqlite"),
        webhook=WEBHOOK,
        transport=wire,
    )
    controller = worker_controller(live_repairs, {1: incident}, fakes()[:2])
    worker = RepairWorker(controller, notifier=notifier)

    controller.consider(incident)
    assert [d.action for d in worker.tick()] == ["dispatched"]
    assert len(wire.calls) == 1
    assert "Investigating ·" in wire.calls[0]["json"]["text"]

    # The next pass only polls a running session: nothing new to say.
    assert [d.action for d in worker.tick()] == ["running"]
    assert len(wire.calls) == 1


def test_a_broken_notifier_cannot_change_a_repair(
    repairs: RepairStore, incident: dict[str, Any], tmp_path: Path
) -> None:
    """Slack is a side effect of the lifecycle, never part of it."""
    from portal.notify import NotificationLog, Notifier

    class Exploding(Notifier):
        def publish(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("slack is down")

    controller = worker_controller(repairs, {1: incident}, fakes()[:2])
    worker = RepairWorker(
        controller,
        notifier=Exploding(log=NotificationLog(tmp_path / "n.sqlite"), webhook=""),
    )
    controller.consider(incident)

    assert [d.action for d in worker.tick()] == ["dispatched"]
    repair = repairs.by_fingerprint(incident["fingerprint"])
    assert repair is not None and repair["session_id"]


def test_a_dispatch_whose_process_died_before_speaking_is_announced_on_restart(
    live_repairs: RepairStore, incident: dict[str, Any], tmp_path: Path
) -> None:
    """The crash window between advance() and the message is real."""
    from portal.notify import NotificationLog, Notifier

    from test_notify import WEBHOOK, Recorder

    ledger = tmp_path / "notifications.sqlite"
    NotificationLog(ledger).watermark("2000-01-01T00:00:00.000+00:00")

    # First process: dispatch commits, then nothing gets said.
    crashed = RepairWorker(worker_controller(live_repairs, {1: incident}, fakes()[:2]))
    crashed.controller.consider(incident)
    assert [d.action for d in crashed.tick()] == ["dispatched"]

    wire = Recorder()
    restarted = RepairWorker(
        worker_controller(live_repairs, {1: incident}, fakes()[:2]),
        notifier=Notifier(
            log=NotificationLog(ledger), webhook=WEBHOOK, transport=wire
        ),
    )

    recovered = restarted.catch_up()

    assert len(recovered) == 1 and len(wire.calls) == 1
    assert "Investigating ·" in wire.calls[0]["json"]["text"]
    # Neither a further restart nor the live pass repeats it: the event id
    # is the same either way, and the ledger already holds it.
    assert restarted.catch_up() == []
    restarted.tick()
    started = [
        call
        for call in wire.calls
        if "Investigating ·" in call["json"]["text"]
    ]
    assert len(started) == 1
