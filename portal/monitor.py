"""Scheduled synthetic browser monitoring.

A small loop that opens one already-saved chart in a browser this process
owns, and writes down what the *product* logged while rendering it. That is
the whole trigger: the schedule decides when to look, and a real console event
captured from the page decides whether anything happens. A clean scan produces
no incident, no issue and no repair session.

What it is not, said plainly so nobody has to infer it:

* It is **not** reading anyone else's browser. The only console it can see is
  the one inside this container. Nothing in Superset, and nothing in a user's
  machine, reports to it.
* It is **read-only**. It signs in, navigates to a saved chart and reads the
  rendered cells. It never changes a format, saves a chart, or touches the
  fixtures — those are one-time setup (`scenarios/b1_fixture.py`) and the
  verifier's replay, both separate from monitoring.
* It **invents nothing**. Console entries are captured as the page emitted
  them, with the product's own severity; qualification into an incident
  happens server-side in `portal.telemetry`, against registered signatures.

Credentials: the monitor holds only its Superset demo login, which it reads
from the environment. It has no GitHub token, no Devin key and no Slack
credential — dispatch lives in the coordinator, on the other side of the
shared state directory — and it puts nothing into page JavaScript.

    python3 -m portal.monitor --once     # one scan, then exit
    python3 -m portal.monitor            # scan every MONITOR_INTERVAL_SECONDS
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import telemetry
from .config import settings
from .events import TELEMETRY, EventStore, utcnow
from .provenance import load as load_provenance
from .provenance import summary as provenance_summary

#: Who the evidence says observed the failure. Never a person: this actor is a
#: managed browser, and the console must be able to say so.
ACTOR = "synthetic-browser-monitor"

#: Long enough for a chart to render on a cold cache, short enough that a hung
#: page ends the scan instead of the loop.
PAGE_TIMEOUT_MS = 60_000


@dataclass(frozen=True)
class MonitorConfig:
    base_url: str
    username: str
    password: str
    chart_id: int
    chart_name: str
    scenario: str
    interval_seconds: int
    name: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "MonitorConfig":
        def value(key: str, default: str) -> str:
            got = env.get(key)
            return default if got is None or got == "" else got

        return cls(
            base_url=value("MONITOR_BASE_URL", settings.superset_base_url).rstrip("/"),
            username=value("MONITOR_USERNAME", settings.upstream_username),
            password=value("MONITOR_PASSWORD", settings.upstream_password),
            chart_id=int(value("MONITOR_CHART_ID", "0")),
            chart_name=value("MONITOR_CHART_NAME", ""),
            scenario=value("MONITOR_SCENARIO", "B1"),
            interval_seconds=int(value("MONITOR_INTERVAL_SECONDS", "300")),
            name=value("MONITOR_NAME", "b1-table-format-monitor"),
        )

    @property
    def route(self) -> str:
        return f"/explore/?slice_id={self.chart_id}"


class Capture:
    """The page's own output, collected verbatim for later qualification."""

    def __init__(self) -> None:
        self.console: list[dict[str, Any]] = []
        self.page_errors: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def attach(self, page: Any) -> None:
        page.on("console", self._console)
        page.on("pageerror", self._page_error)
        page.on("response", self._response)

    def _console(self, message: Any) -> None:
        self.console.append(
            {
                "ts": utcnow(),
                # The product's severity, untranslated. A warning stays a
                # warning all the way to the incident.
                "severity": message.type,
                "text": message.text,
                "location": dict(message.location or {}),
            }
        )

    def _page_error(self, error: Any) -> None:
        self.page_errors.append({"ts": utcnow(), "severity": "error", "text": str(error)})

    def _response(self, response: Any) -> None:
        url = response.url
        if "/api/v1/chart/data" not in url:
            return
        self.requests.append(
            {
                "method": response.request.method,
                "path": urlsplit(url).path,
                "status": response.status,
            }
        )


def _sign_in(page: Any, config: MonitorConfig) -> None:
    page.goto(f"{config.base_url}/login/", timeout=PAGE_TIMEOUT_MS)
    if page.locator("input#username, input[name=username]").count():
        page.locator("input#username, input[name=username]").first.fill(config.username)
        page.locator("input#password, input[name=password]").first.fill(config.password)
        page.locator("button:has-text('Sign in'), input[type=submit]").first.click()
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)


def _visible_values(page: Any) -> str:
    """A short read of what the chart is showing, for the evidence bundle.

    Reading only: no control is touched, so a scan leaves the saved chart
    exactly as the fixture saved it.
    """
    try:
        cells = page.locator("div[data-test='chart-container'] table td")
        texts = [cells.nth(i).inner_text().strip() for i in range(min(cells.count(), 12))]
    except Exception:  # noqa: BLE001 - a missing table is not a monitor failure
        return ""
    return " | ".join(t for t in texts if t)[:400]


def scan(config: MonitorConfig, *, browser: Any) -> dict[str, Any]:
    """One read-only visit to the saved chart, with everything it logged."""
    capture = Capture()
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    capture.attach(page)
    started = utcnow()
    error = ""
    values = ""
    try:
        _sign_in(page, config)
        page.goto(f"{config.base_url}{config.route}", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(5_000)
        values = _visible_values(page)
    except Exception as exc:  # noqa: BLE001 - a bad scan must not kill the loop
        error = type(exc).__name__
    finally:
        context.close()
    return {
        "scan_id": f"scan_{uuid.uuid4().hex[:12]}",
        "monitor": config.name,
        "started_at": started,
        "finished_at": utcnow(),
        "route": config.route,
        "chart": config.chart_name or f"slice {config.chart_id}",
        "visible_values": values,
        "console": capture.console + capture.page_errors,
        "requests": capture.requests,
        "navigation_error": error,
    }


def record(result: dict[str, Any], events: EventStore, config: MonitorConfig) -> dict[str, Any]:
    """Qualify one scan and write it to the shared event log.

    Two kinds of row, never confused: a scan summary (`ok`) that says the check
    ran, and one `telemetry` event per consolidated finding. Only the second
    kind can become an incident, and only if a registered signature matched.
    """
    summary = telemetry.qualify(result["console"])
    revision = provenance_summary(load_provenance(settings.provenance_path))
    trace_id = result["scan_id"]
    page = {
        "chart": result["chart"],
        "route": result["route"],
        "visible_values": result["visible_values"],
    }
    base = {
        "trace_id": trace_id,
        "scenario": config.scenario,
        "actor": ACTOR,
        "environment_kind": settings.environment_kind,
        "run_id": settings.run_id,
        "revision": revision,
    }
    events.emit(
        {
            **base,
            "step_index": 0,
            "kind": "monitor_scan",
            "outcome": "ok",
            "operation": "monitor.scan",
            "message": (
                f"read-only scan of {page['chart']}"
                + (f" (navigation error: {result['navigation_error']})" if result["navigation_error"] else "")
            ),
            "input": {"route": page["route"], "read_only": True, "monitor": config.name},
            "output": {
                **summary.as_dict(),
                "chart": page["chart"],
                "visible_values": page["visible_values"],
                "requests": result["requests"][:10],
            },
        }
    )
    for index, finding in enumerate(summary.findings, start=1):
        if finding.classification == telemetry.IGNORED:
            continue
        events.emit(
            {
                **base,
                "step_index": index,
                "kind": "browser_telemetry",
                "outcome": TELEMETRY,
                "operation": "browser.console",
                "message": f"{finding.severity}: {finding.message}"[:300],
                "input": {"route": page["route"], "read_only": True},
                "output": telemetry.telemetry_event(
                    finding,
                    page=page,
                    requests=result["requests"],
                    scan={"monitor": config.name, "scan_id": result["scan_id"]},
                ),
            }
        )
    return {
        "scan_id": result["scan_id"],
        "monitor": config.name,
        "chart": page["chart"],
        "visible_values": page["visible_values"],
        "navigation_error": result["navigation_error"],
        **summary.as_dict(),
    }


def run(config: MonitorConfig, *, once: bool = False, data_dir: Path | None = None) -> int:
    from playwright.sync_api import sync_playwright  # imported late: only the monitor needs it

    if config.chart_id <= 0:
        raise SystemExit("MONITOR_CHART_ID must name the saved chart to watch")
    events = EventStore((data_dir or settings.data_dir) / "events.sqlite", stream=sys.stderr)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        try:
            while True:
                report = record(scan(config, browser=browser), events, config)
                print(json.dumps(report, sort_keys=True), flush=True)
                if once:
                    return 0
                time.sleep(config.interval_seconds)
        finally:
            browser.close()


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run a single scan and exit")
    args = parser.parse_args(argv)
    return run(MonitorConfig.from_env(dict(os.environ)), once=args.once)


if __name__ == "__main__":
    sys.exit(main())
