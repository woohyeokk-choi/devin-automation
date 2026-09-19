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


def keys_on_home(base: str, session: requests.Session) -> set[str]:
    page = session.get(f"{base}/").text
    return {chunk.split('"')[0] for chunk in page.split('href="/explorations/')[1:]}


def s2(base: str, session: requests.Session) -> dict[str, str]:
    """Save a link, discard it, re-save different settings in the same tab."""
    before = keys_on_home(base, session)
    first = session.post(
        f"{base}/explorations",
        data={"dimension": "channel", "sort": "asc", "row_limit": 25},
    )
    new_keys = keys_on_home(base, session) - before
    if not new_keys:
        raise SystemExit("the first exploration was not saved — is the stack up?")
    key = new_keys.pop()
    traces = {"save_first": trace_of(first)}
    traces["open_first"] = trace_of(session.get(f"{base}/explorations/{key}"))
    traces["discard"] = trace_of(session.post(f"{base}/explorations/{key}/discard"))
    traces["open_after_discard"] = trace_of(session.get(f"{base}/explorations/{key}"))
    traces["save_second"] = trace_of(
        session.post(
            f"{base}/explorations",
            data={"dimension": "product", "sort": "desc", "row_limit": 10},
        )
    )
    traces["reopen_stale_link"] = trace_of(session.get(f"{base}/explorations/{key}"))
    return traces


def s1(base: str, session: requests.Session) -> dict[str, str]:
    """Restore the row limit explicitly, then change only the sort."""
    restore = session.post(
        f"{base}/settings/sort", data={"descending": "true", "explicit_row_limit": "137"}
    )
    sort_only = session.post(
        f"{base}/settings/sort", data={"descending": "false", "explicit_row_limit": ""}
    )
    return {
        "explicit_row_limit_control": trace_of(restore),
        "sort_only_change": trace_of(sort_only),
        "read_back": trace_of(session.get(f"{base}/settings")),
    }


def n1(base: str, session: requests.Session) -> dict[str, str]:
    session.post(f"{base}/profile", data={"profile": "restricted_viewer"})
    traces = {
        "dashboard": trace_of(session.get(f"{base}/")),
        "save_attempt": trace_of(
            session.post(
                f"{base}/explorations",
                data={"dimension": "region", "sort": "desc", "row_limit": 10},
            )
        ),
        "settings_attempt": trace_of(session.get(f"{base}/settings")),
    }
    session.post(f"{base}/profile", data={"profile": "analyst"})
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


def verdict(scenario: str, traces: dict[str, str], events: list[dict]) -> dict:
    """Machine-readable split: baseline defects vs the harness's own health.

    A reproduced baseline defect is the point of the run, so it must not be
    counted as a failing automation test. Only harness assertions and
    blocked/error steps say the replay itself is unhealthy.
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
    return {
        "scenario": scenario,
        "traces": traces,
        "events": len(events),
        "baseline_defect_reproduced": sorted(set(defects)),
        "expected_denials": sum(e["outcome"] == "expected_denial" for e in events),
        "harness_failures": sorted(set(harness_failures)),
        "blocked_steps": sorted(set(stuck)),
        "not_applicable": NOT_APPLICABLE.get(scenario, []),
        "harness_healthy": not harness_failures and not stuck,
    }


def export(base: str, auth: tuple[str, str], traces: dict[str, str], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for trace_id in dict.fromkeys(traces.values()):
        if not trace_id:
            continue
        response = requests.get(
            f"{base}/ops/export.jsonl", params={"trace_id": trace_id}, auth=auth
        )
        response.raise_for_status()
        lines.extend(line for line in response.text.splitlines() if line.strip())
    out.write_text("\n".join(lines) + "\n")
    events = [json.loads(line) for line in lines]
    (out.parent / "verdict.json").write_text(
        json.dumps(verdict(out.parent.name, traces, events), indent=2) + "\n"
    )
    return len(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8090")
    parser.add_argument("--ops-user", default="operator")
    parser.add_argument("--ops-password", default="operator-local")
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "examples"))
    args = parser.parse_args()

    auth = (args.ops_user, args.ops_password)
    out = Path(args.out)
    reset = requests.post(f"{args.base}/ops/fixtures/reset", auth=auth)
    reset.raise_for_status()
    print("fixture reset to the documented starting state")
    for name, scenario in (("S2", s2), ("S1", s1), ("N1", n1)):
        session = requests.Session()
        traces = scenario(args.base, session)
        path = out / name / "events.redacted.jsonl"
        count = export(args.base, auth, traces, path)
        summary = json.loads((path.parent / "verdict.json").read_text())
        print(
            f"{name}: {len(traces)} actions, {count} events, "
            f"baseline defects {summary['baseline_defect_reproduced']}, "
            f"harness healthy {summary['harness_healthy']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
