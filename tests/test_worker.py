"""Provider wiring and the background poller, with no live call anywhere.

The fakes are injected explicitly. Missing credentials must be an error the
deployment sees at start-up, never a quiet fall back to a simulated success.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from portal.controller import NEEDS_ATTENTION, Controller, RepairStore
from portal.incidents import IncidentStore
from portal.providers import Devin, GitHub, NotConfigured
from portal.simulation import FakeDevin, FakeGitHub, FakeTransport
from portal.worker import RepairPoller, build_controller, live_providers

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


def test_enabled_dispatch_uses_the_injected_providers(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    github, devin, _, devin_api = fakes()
    controller = build_controller(
        repairs,
        target_repo=REPO,
        versions=VERSIONS,
        dispatch_enabled=True,
        providers=(github, devin),
    )
    assert controller.consider(incident).action == "dispatched"
    assert len(devin_api.sessions) == 1


def test_the_poller_records_a_rejected_poll_instead_of_raising(
    repairs: RepairStore, incident: dict[str, Any]
) -> None:
    github, devin, _, devin_api = fakes()
    controller = build_controller(
        repairs,
        target_repo=REPO,
        versions=VERSIONS,
        dispatch_enabled=True,
        providers=(github, devin),
    )
    controller.consider(incident)

    for status in (401, 403, 429):
        devin_api.status = status
        decisions = RepairPoller(controller).tick()
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
