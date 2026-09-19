#!/usr/bin/env python3
"""Drive the portal the way the browser does and export the resulting traces.

Same HTTP surface as the click-through (server-rendered forms, one cookie
session, one workspace), so the exported JSONL is the portal's own log of a
real run — not a re-implementation of the scenarios. Written to
`artifacts/examples/{S2,S1,N1}/events.redacted.jsonl`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
RESET_ASSERTION = "fixture_reset_reaches_the_documented_starting_state"

# What a replay must actually have produced. A run that is missing any of
# these did not exercise the scenario, so it cannot report a healthy harness
# however few failures it happens to contain.
REQUIRED_TRACES = {
    "S2": (
        "save_first",
        "open_first",
        "discard",
        "open_after_discard",
        "save_second",
        "reopen_stale_link",
    ),
    "S1": ("explicit_row_limit_control", "sort_only_change", "read_back"),
    "N1": ("dashboard", "save_attempt", "settings_attempt"),
}
REQUIRED_ASSERTIONS = {
    "S2": (
        "discard_removes_the_exploration_immediately",
        "discarded_exploration_link_stays_gone",
        "new_exploration_does_not_reuse_a_discarded_key",
    ),
    "S1": (
        "requested_sort_change_took_effect",
        "row_limit_survives_an_unrelated_change",
        "color_scheme_survives_an_unrelated_change",
        "explicit_row_limit_is_applied",
    ),
    "N1": (),
}


class Client:
    """The portal's own HTTP surface, behind the demo gate.

    Every state-changing form carries the CSRF token the server issued, so
    this script goes through exactly the checks a browser does rather than
    around them.
    """

    def __init__(self, base: str, auth: tuple[str, str]) -> None:
        self.base = base
        self.session = requests.Session()
        self.session.auth = auth

    def get(self, path: str, **kwargs: object) -> requests.Response:
        return self.session.get(f"{self.base}{path}", **kwargs)  # type: ignore[arg-type]

    def post(self, path: str, data: dict | None = None) -> requests.Response:
        token = self.session.cookies.get("portal_csrf")
        if not token:  # the token is issued with the first rendered page
            self.get("/")
            token = self.session.cookies.get("portal_csrf")
        return self.session.post(
            f"{self.base}{path}",
            data={**(data or {}), "csrf_token": token},
            headers={"Origin": self.base, "Referer": f"{self.base}/"},
        )


def trace_of(response: requests.Response) -> str:
    """Find the trace a request produced.

    Actions redirect with `?trace_id=`; plain page views link their own trace
    in the page header, which is what the operator clicks.
    """
    for item in [response, *response.history][::-1]:
        query = parse_qs(urlparse(item.headers.get("Location", "")).query)
        if query.get("trace_id"):
            return query["trace_id"][0]
    query = parse_qs(urlparse(response.url).query)
    if query.get("trace_id"):
        return query["trace_id"][0]
    marker = '/ops/traces/'
    if marker in response.text:
        return response.text.split(marker, 1)[1].split('"', 1)[0]
    return ""


def keys_on_home(client: Client) -> set[str]:
    page = client.get("/").text
    return {chunk.split('"')[0] for chunk in page.split('href="/explorations/')[1:]}


def s2(client: Client) -> dict[str, str]:
    """Save a link, discard it, re-save different settings in the same tab."""
    before = keys_on_home(client)
    first = client.post(
        "/explorations", {"dimension": "channel", "sort": "asc", "row_limit": 25}
    )
    new_keys = keys_on_home(client) - before
    if not new_keys:
        raise SystemExit("the first exploration was not saved — is the stack up?")
    key = new_keys.pop()
    traces = {"save_first": trace_of(first)}
    traces["open_first"] = trace_of(client.get(f"/explorations/{key}"))
    traces["discard"] = trace_of(client.post(f"/explorations/{key}/discard"))
    traces["open_after_discard"] = trace_of(client.get(f"/explorations/{key}"))
    traces["save_second"] = trace_of(
        client.post("/explorations", {"dimension": "product", "sort": "desc", "row_limit": 10})
    )
    traces["reopen_stale_link"] = trace_of(client.get(f"/explorations/{key}"))
    return traces


def s1(client: Client) -> dict[str, str]:
    """Restore the row limit explicitly, then change only the sort."""
    restore = client.post(
        "/settings/sort", {"descending": "true", "explicit_row_limit": "137"}
    )
    sort_only = client.post(
        "/settings/sort", {"descending": "false", "explicit_row_limit": ""}
    )
    return {
        "explicit_row_limit_control": trace_of(restore),
        "sort_only_change": trace_of(sort_only),
        "read_back": trace_of(client.get("/settings")),
    }


def n1(client: Client) -> dict[str, str]:
    client.post("/profile", {"profile": "restricted_viewer"})
    traces = {
        "dashboard": trace_of(client.get("/")),
        "save_attempt": trace_of(
            client.post("/explorations", {"dimension": "region", "sort": "desc", "row_limit": 10})
        ),
        "settings_attempt": trace_of(client.get("/settings")),
    }
    client.post("/profile", {"profile": "analyst"})
    return traces


NOT_APPLICABLE = {
    "N1": [
        {
            "case": "restricted profile submits the chart sort form",
            "status": "not_applicable",
            "reason": (
                "the settings page is denied to this profile, so the sort form is "
                "never rendered; the denial itself is the covered behaviour and the "
                "form is not exposed to force coverage"
            ),
        }
    ]
}


def missing_evidence(
    scenario: str, traces: dict[str, str], events: list[dict], reset_verified: bool
) -> list[str]:
    """Everything the replay was supposed to record and did not.

    Absent evidence is not a passing run: a scenario whose trace never came
    back, or whose contract was never asserted, proves nothing about the
    product either way.
    """
    gaps = [
        f"trace:{name}"
        for name in REQUIRED_TRACES[scenario]
        if not (traces.get(name) or "").strip()
    ]
    recorded = {e["assertion"]["name"] for e in events if e.get("assertion")}
    gaps += [
        f"assertion:{name}"
        for name in REQUIRED_ASSERTIONS[scenario]
        if name not in recorded
    ]
    if scenario == "N1" and not any(e["outcome"] == "expected_denial" for e in events):
        gaps.append("outcome:expected_denial")
    if not reset_verified:
        gaps.append(f"assertion:{RESET_ASSERTION}")
    return gaps


def verdict(
    scenario: str,
    traces: dict[str, str],
    events: list[dict],
    reset_verified: bool = False,
) -> dict:
    """Machine-readable split: baseline defects vs the harness's own health.

    A reproduced baseline defect is the point of the run, so it must not be
    counted as a failing automation test. Only harness assertions, blocked or
    errored steps and missing evidence say the replay itself is unhealthy.
    """
    assertions = [e for e in events if e.get("assertion")]
    defects = [
        e["assertion"]["name"]
        for e in assertions
        if e["outcome"] == "assertion_failed"
        and e["assertion"].get("known_baseline_defect")
    ]
    harness_failures = [
        e["assertion"]["name"]
        for e in assertions
        if e["outcome"] == "assertion_failed"
        and not e["assertion"].get("known_baseline_defect")
    ]
    stuck = [e["operation"] for e in events if e["outcome"] in ("blocked", "error")]
    gaps = missing_evidence(scenario, traces, events, reset_verified)
    return {
        "scenario": scenario,
        "traces": traces,
        "events": len(events),
        "baseline_defect_reproduced": sorted(set(defects)),
        "expected_denials": sum(e["outcome"] == "expected_denial" for e in events),
        "harness_failures": sorted(set(harness_failures)),
        "blocked_steps": sorted(set(stuck)),
        "missing_evidence": gaps,
        "fixture_reset_verified": reset_verified,
        "not_applicable": NOT_APPLICABLE.get(scenario, []),
        "harness_healthy": not harness_failures and not stuck and not gaps,
    }


def events_of(ops: Client, traces: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for trace_id in dict.fromkeys(t for t in traces.values() if t.strip()):
        response = ops.get("/ops/export.jsonl", params={"trace_id": trace_id})
        response.raise_for_status()
        lines.extend(line for line in response.text.splitlines() if line.strip())
    return lines


def reset_fixture(ops: Client) -> bool:
    """Reset, and require the recorded assertion — not the redirect — as proof.

    A 200 after a redirect only says the route ran; the starting state is
    proven by the harness assertion the reset itself recorded.
    """
    response = ops.post("/ops/fixtures/reset")
    response.raise_for_status()
    trace_id = trace_of(response)
    if not trace_id:
        return False
    for line in events_of(ops, {"reset": trace_id}):
        event = json.loads(line)
        assertion = event.get("assertion") or {}
        if assertion.get("name") == RESET_ASSERTION and assertion.get("holds"):
            return True
    return False


def export(ops: Client, traces: dict[str, str], out: Path, reset_verified: bool) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = events_of(ops, traces)
    out.write_text("\n".join(lines) + "\n")
    events = [json.loads(line) for line in lines]
    summary = verdict(out.parent.name, traces, events, reset_verified)
    (out.parent / "verdict.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8090")
    parser.add_argument("--ops-user", default="operator")
    parser.add_argument("--ops-password", default="operator-local")
    parser.add_argument("--demo-user", default="demo")
    parser.add_argument("--demo-password", default="demo-local")
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "examples"))
    args = parser.parse_args()

    ops = Client(args.base, (args.ops_user, args.ops_password))
    out = Path(args.out)
    reset_verified = reset_fixture(ops)
    print(
        "fixture reset to the documented starting state"
        if reset_verified
        else "fixture reset did NOT record its starting-state assertion"
    )

    unhealthy = []
    for name, scenario in (("S2", s2), ("S1", s1), ("N1", n1)):
        traces = scenario(Client(args.base, (args.demo_user, args.demo_password)))
        path = out / name / "events.redacted.jsonl"
        summary = export(ops, traces, path, reset_verified)
        print(
            f"{name}: {len(traces)} actions, {summary['events']} events, "
            f"baseline defects {summary['baseline_defect_reproduced']}, "
            f"harness healthy {summary['harness_healthy']}"
            + (f", missing {summary['missing_evidence']}" if summary["missing_evidence"] else "")
        )
        if not summary["harness_healthy"]:
            unhealthy.append(name)
    if unhealthy:
        print(f"incomplete or unhealthy replay: {', '.join(unhealthy)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
