"""The independent validator: fixed assertions replayed against a stack.

This is the trusted half of verification. It never reads anything from the
candidate checkout — only its HTTP and MCP surfaces — and it lives in the
automation repository at a pinned revision, so a repair session cannot change
the thing that grades it.

Three verdicts, and only three:

``passed``
    every registered check for the selected case held, controls included.
``failed``
    the product contract was exercised and did not hold. This is the only
    outcome that produces feedback to a session.
``blocked``
    the run could not answer the question: setup failed, a service was
    unreachable, authentication was lost, a check never executed. A blocked
    run is never a pass and never a product failure.

Run it directly against any stack::

    python3 -m portal.validator --case S2 \\
        --base-url http://127.0.0.1:8088 --mcp-url http://127.0.0.1:5008/mcp
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from clients.mcp_client import MCPClient, MCPError
from clients.superset_client import SupersetClient

PASSED, FAILED, BLOCKED = "passed", "failed", "blocked"
TARGET, CONTROL, SETUP = "target", "control", "setup"

DATASET_TABLE = "synthetic_orders"
CHART_NAME = "Revenue by region (runtime-repair fixture)"
BASELINE_ROW_LIMIT = 137
SCHEMA_DEFAULT_ROW_LIMIT = 1000
COLOR_SCHEME = "googleCategory10c"
#: What the MCP service must expose before S1 means anything. The server
#: publishes a small meta surface (`call_tool` dispatches the chart tools by
#: name), so this is the discovery check, not a list of chart tools.
REQUIRED_MCP_TOOLS = ("call_tool", "search_tools")

#: Which cases answer which registered failure family. One repair targets one
#: defect: a candidate cut from the immutable baseline still carries the other
#: known defect, so the other family's target case is deliberately absent.
CASES_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "discarded_form_data_key_is_reused": ("S2", "N1"),
    "omitted_row_limit_is_reset": ("S1", "N1"),
}


class Blocked(Exception):
    """Setup, environment or authentication failure. Never a verdict."""


@dataclass
class Check:
    name: str
    kind: str
    expected: Any
    observed: Any
    holds: bool
    note: str = ""


@dataclass
class CaseResult:
    case: str
    verdict: str
    checks: list[Check] = field(default_factory=list)
    blocked_reason: str = ""
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checks"] = [asdict(check) for check in self.checks]
        return data


class Recorder:
    """Collects checks and decides the verdict from them, not from a summary."""

    def __init__(self, case: str) -> None:
        self.case = case
        self.checks: list[Check] = []
        self.facts: dict[str, Any] = {}

    def check(
        self, name: str, kind: str, expected: Any, observed: Any, note: str = ""
    ) -> bool:
        holds = expected == observed
        self.checks.append(Check(name, kind, expected, observed, holds, note))
        return holds

    def require(self, name: str, expected: Any, observed: Any) -> None:
        """A precondition. Failing it blocks the case instead of failing it."""
        if not self.check(name, SETUP, expected, observed):
            raise Blocked(f"{name}: expected {expected!r}, observed {observed!r}")

    def result(self) -> CaseResult:
        graded = [c for c in self.checks if c.kind in (TARGET, CONTROL)]
        if not graded:
            return CaseResult(
                self.case, BLOCKED, self.checks, "no assertion executed", self.facts
            )
        verdict = PASSED if all(c.holds for c in graded) else FAILED
        return CaseResult(self.case, verdict, self.checks, "", self.facts)


def _blocked(case: str, reason: str, recorder: Recorder | None = None) -> CaseResult:
    checks = recorder.checks if recorder else []
    facts = recorder.facts if recorder else {}
    return CaseResult(case, BLOCKED, checks, reason, facts)


# ----------------------------------------------------------------- plumbing
@dataclass
class Target:
    """Where to replay. Credentials are the stack's own demo accounts."""

    base_url: str
    mcp_url: str
    username: str = "admin"
    password: str = "admin"
    restricted_username: str = "restricted_analyst"
    restricted_password: str = "restricted-analyst-local"


def _login(target: Target, username: str, password: str) -> SupersetClient:
    client = SupersetClient(target.base_url)
    try:
        client.login(username, password)
    except (requests.RequestException, RuntimeError) as exc:
        raise Blocked(f"could not authenticate {username}: {type(exc).__name__}") from None
    return client


def _dataset_id(client: SupersetClient) -> int:
    try:
        dataset_id = client.find_dataset(DATASET_TABLE)
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise Blocked(f"dataset lookup failed: {type(exc).__name__}") from None
    if dataset_id is None:
        raise Blocked(f"the {DATASET_TABLE} fixture is missing from this stack")
    return int(dataset_id)


def _form_data(dataset_id: int, dimension: str, row_limit: int) -> dict[str, Any]:
    return {
        "datasource": f"{dataset_id}__table",
        "viz_type": "table",
        "groupby": [dimension],
        "metrics": ["count"],
        "row_limit": row_limit,
    }


def _payload(dataset_id: int, form_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "datasource_id": dataset_id,
        "datasource_type": "table",
        "form_data": json.dumps(form_data),
    }


def _key_of(response: requests.Response) -> str:
    try:
        return str(response.json()["key"])
    except (ValueError, KeyError) as exc:
        raise Blocked(f"the form-data API returned no key ({type(exc).__name__})") from None


def _stored_form_data(response: requests.Response) -> dict[str, Any] | None:
    if response.status_code != 200:
        return None
    try:
        return dict(json.loads(response.json()["form_data"]))
    except (ValueError, KeyError, TypeError):
        return None


# -------------------------------------------------------------------- cases
def case_s2(target: Target) -> CaseResult:
    """S2 — a discarded exploration key must not come back.

    One authenticated session, one tab, the exact sequence a user performs:
    save, open, discard, then start another exploration. The controls keep a
    fix honest: a different tab still gets its own key, and saving twice in
    one context without discarding still updates in place, which is the
    source-defined behaviour. Simply never reusing a key would break that.
    """
    recorder = Recorder("S2")
    try:
        client = _login(target, target.username, target.password)
        dataset_id = _dataset_id(client)
        tab = str(uuid.uuid4().int % 900000 + 100000)
        a = _form_data(dataset_id, "region", 100)
        b = _form_data(dataset_id, "product", 250)

        created_a = client.post(
            f"/api/v1/explore/form_data?tab_id={tab}", json=_payload(dataset_id, a)
        )
        recorder.require("create_a_succeeds", 201, created_a.status_code)
        k1 = _key_of(created_a)

        read_a = client.get(f"/api/v1/explore/form_data/{k1}")
        recorder.require("read_a_succeeds", 200, read_a.status_code)
        recorder.require("read_a_returns_exactly_a", a, _stored_form_data(read_a))

        discarded = client.delete(f"/api/v1/explore/form_data/{k1}")
        recorder.require("discard_succeeds", 200, discarded.status_code)
        recorder.require(
            "discarded_key_is_immediately_gone",
            404,
            client.get(f"/api/v1/explore/form_data/{k1}").status_code,
        )

        created_b = client.post(
            f"/api/v1/explore/form_data?tab_id={tab}", json=_payload(dataset_id, b)
        )
        recorder.require("create_b_succeeds", 201, created_b.status_code)
        k2 = _key_of(created_b)
        recorder.facts.update(tab_id=tab, k1=k1, k2=k2, dataset_id=dataset_id)

        recorder.check(
            "new_exploration_does_not_reuse_a_discarded_key",
            TARGET,
            False,
            k1 == k2,
            "the key handed to the next exploration must not be the discarded one",
        )
        after_b = client.get(f"/api/v1/explore/form_data/{k1}")
        recorder.check(
            "discarded_link_stays_dead_after_a_new_exploration",
            TARGET,
            404,
            after_b.status_code,
            "the old link must not resolve again, and must not show the new state",
        )
        read_b = client.get(f"/api/v1/explore/form_data/{k2}")
        recorder.check(
            "the_new_exploration_reads_back_exactly_what_was_saved",
            TARGET,
            b,
            _stored_form_data(read_b),
            "",
        )

        # Control: a different tab is a different workspace, always was.
        other_tab = str(uuid.uuid4().int % 900000 + 100000)
        c = _form_data(dataset_id, "channel", 75)
        created_c = client.post(
            f"/api/v1/explore/form_data?tab_id={other_tab}", json=_payload(dataset_id, c)
        )
        recorder.require("control_create_in_another_tab_succeeds", 201, created_c.status_code)
        k3 = _key_of(created_c)
        recorder.check(
            "control_a_second_workspace_gets_its_own_key", CONTROL, False, k3 == k2
        )
        recorder.check(
            "control_a_second_workspace_reads_its_own_state",
            CONTROL,
            c,
            _stored_form_data(client.get(f"/api/v1/explore/form_data/{k3}")),
        )

        # Control: saving twice in one context *without* discarding keeps the
        # same key and updates it. That is the contract; a fix must keep it.
        reuse_tab = str(uuid.uuid4().int % 900000 + 100000)
        d1 = _form_data(dataset_id, "region", 11)
        d2 = _form_data(dataset_id, "region", 22)
        first = client.post(
            f"/api/v1/explore/form_data?tab_id={reuse_tab}", json=_payload(dataset_id, d1)
        )
        recorder.require("control_first_save_succeeds", 201, first.status_code)
        k4 = _key_of(first)
        second = client.post(
            f"/api/v1/explore/form_data?tab_id={reuse_tab}", json=_payload(dataset_id, d2)
        )
        recorder.require("control_second_save_succeeds", 201, second.status_code)
        recorder.check(
            "control_same_context_save_without_discarding_updates_in_place",
            CONTROL,
            True,
            _key_of(second) == k4,
            "unchanged source behaviour: same tab and datasource, no discard",
        )
        recorder.check(
            "control_same_context_reuse_serves_the_latest_state",
            CONTROL,
            d2,
            _stored_form_data(client.get(f"/api/v1/explore/form_data/{k4}")),
        )
    except Blocked as exc:
        return _blocked("S2", str(exc), recorder)
    except requests.RequestException as exc:
        return _blocked("S2", f"transport error: {type(exc).__name__}", recorder)
    return recorder.result()


def mcp_readiness(target: Target) -> dict[str, Any]:
    """Protocol-level readiness of the MCP listener, independent of S1.

    The web service answering `/health` says nothing about the MCP process:
    it is a different container with a different listener. So this speaks
    MCP — `initialize` then `tools/list` — and reports what the server said
    about itself.
    """
    try:
        client, info, tools = _mcp_ready(target)
    except Blocked as exc:
        return {"ready": False, "reason": str(exc), "tools": [], "server": {}}
    del client
    return {
        "ready": True,
        "reason": "",
        "tools": tools,
        "server": info.get("serverInfo") or {},
        "protocol_version": info.get("protocolVersion", ""),
    }


def _mcp_ready(target: Target) -> tuple[MCPClient, dict[str, Any], list[str]]:
    client = MCPClient(target.mcp_url)
    try:
        info = client.initialize()
        tools = client.list_tools()
    except (MCPError, requests.RequestException) as exc:
        raise Blocked(f"the MCP service is not ready: {type(exc).__name__}") from None
    missing = [tool for tool in REQUIRED_MCP_TOOLS if tool not in tools]
    if missing:
        raise Blocked(f"the MCP service does not expose {', '.join(missing)}")
    if not (info.get("serverInfo") or {}).get("name"):
        raise Blocked("the MCP service did not identify itself on initialize")
    return client, info, tools


def _call_tool(mcp: MCPClient, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    # The server publishes a meta surface: `call_tool` dispatches the chart
    # tools by name, so the tool name travels in the arguments.
    try:
        result = mcp.call_tool("call_tool", {"name": tool, "arguments": {"request": arguments}})
    except (MCPError, requests.RequestException) as exc:
        raise Blocked(f"MCP {tool} failed: {type(exc).__name__}") from None
    if result.get("isError") or str(result.get("text") or "").startswith("Error"):
        raise Blocked(f"MCP {tool} reported an error")
    return result


def _chart(client: SupersetClient, chart_id: int) -> dict[str, Any]:
    response = client.get(f"/api/v1/chart/{chart_id}")
    if response.status_code != 200:
        raise Blocked(f"chart {chart_id} unreadable (HTTP {response.status_code})")
    params = json.loads(response.json()["result"]["params"])
    order_by = params.get("order_by_cols") or []
    sort: tuple[str | None, bool | None] = (None, None)
    for entry in order_by:
        try:
            column, ascending = json.loads(entry) if isinstance(entry, str) else entry
        except (ValueError, TypeError):
            continue
        sort = (str(column), bool(ascending))
        break
    return {
        "row_limit": params.get("row_limit"),
        "color_scheme": params.get("color_scheme"),
        "sort_by": sort[0],
        "ascending": sort[1],
        "params": params,
    }


def _columns() -> list[dict[str, Any]]:
    return [{"name": "region"}, {"name": "revenue", "aggregate": "SUM"}]


def case_s1(target: Target) -> CaseResult:
    """S1 — an MCP update must not reset the fields the caller omitted.

    The chart is built fresh for the run, so the case does not depend on
    leftovers, and the update changes only the sort. Controls hold the rest of
    the contract in place: an explicit row limit is honoured (including when
    it equals the schema default, where "honoured" and "reset" look alike),
    the colour palette survives, and a new chart still gets its default.
    """
    recorder = Recorder("S1")
    try:
        client = _login(target, target.username, target.password)
        dataset_id = _dataset_id(client)
        mcp, server_info, mcp_tools = _mcp_ready(target)
        recorder.facts["mcp_tools"] = mcp_tools
        recorder.facts["mcp_server"] = server_info.get("serverInfo") or {}

        name = f"{CHART_NAME} {uuid.uuid4().hex[:8]}"
        created = _call_tool(
            mcp,
            "generate_chart",
            {
                "dataset_id": dataset_id,
                "chart_name": name,
                "save_chart": True,
                "generate_preview": False,
                "config": {
                    "chart_type": "table",
                    "columns": _columns(),
                    "sort_by": [{"column": "SUM(revenue)", "ascending": True}],
                    "row_limit": BASELINE_ROW_LIMIT,
                    "color_scheme": COLOR_SCHEME,
                },
            },
        )
        chart_id = int((created.get("chart") or {}).get("id") or 0)
        recorder.require("chart_is_created", True, chart_id > 0)
        before = _chart(client, chart_id)
        recorder.require("chart_starts_at_the_saved_row_limit", BASELINE_ROW_LIMIT, before["row_limit"])
        recorder.require("chart_starts_with_the_saved_palette", COLOR_SCHEME, before["color_scheme"])
        recorder.facts.update(chart_id=chart_id, dataset_id=dataset_id)

        # The user action: change only the sort. row_limit is not mentioned.
        _call_tool(
            mcp,
            "update_chart",
            {
                "identifier": chart_id,
                "generate_preview": False,
                "config": {
                    "chart_type": "table",
                    "columns": _columns(),
                    "sort_by": [{"column": "SUM(revenue)", "ascending": False}],
                },
            },
        )
        after = _chart(client, chart_id)
        recorder.check(
            "the_requested_sort_change_persists", TARGET, False, after["ascending"]
        )
        recorder.check(
            "an_omitted_row_limit_keeps_the_saved_value",
            TARGET,
            BASELINE_ROW_LIMIT,
            after["row_limit"],
            "the caller never mentioned row_limit; it must not fall back to the schema default",
        )
        recorder.check(
            "an_omitted_palette_keeps_the_saved_value", CONTROL, COLOR_SCHEME, after["color_scheme"]
        )

        # Control: an explicit row limit is applied, including the value that
        # happens to be the schema default.
        for control_name, value in (
            ("control_an_explicit_row_limit_is_applied", 275),
            ("control_an_explicit_schema_default_row_limit_is_applied", SCHEMA_DEFAULT_ROW_LIMIT),
        ):
            _call_tool(
                mcp,
                "update_chart",
                {
                    "identifier": chart_id,
                    "generate_preview": False,
                    "config": {
                        "chart_type": "table",
                        "columns": _columns(),
                        "row_limit": value,
                    },
                },
            )
            recorder.check(control_name, CONTROL, value, _chart(client, chart_id)["row_limit"])

        # Control: creating a chart without a row limit still gets the
        # documented default rather than nothing.
        plain = _call_tool(
            mcp,
            "generate_chart",
            {
                "dataset_id": dataset_id,
                "chart_name": f"{name} default",
                "save_chart": True,
                "generate_preview": False,
                "config": {"chart_type": "table", "columns": _columns()},
            },
        )
        plain_id = int((plain.get("chart") or {}).get("id") or 0)
        recorder.require("control_plain_chart_is_created", True, plain_id > 0)
        recorder.check(
            "control_a_new_chart_keeps_the_schema_default_row_limit",
            CONTROL,
            SCHEMA_DEFAULT_ROW_LIMIT,
            _chart(client, plain_id)["row_limit"],
        )
    except Blocked as exc:
        return _blocked("S1", str(exc), recorder)
    except requests.RequestException as exc:
        return _blocked("S1", f"transport error: {type(exc).__name__}", recorder)
    return recorder.result()


N1_TAB = "552266"


def case_n1(target: Target) -> CaseResult:
    """N1 — the restricted role is denied, and the denial is authenticated.

    A 403 from a logged-in Gamma user is the control passing. A 401 is not:
    it says the session was never established or was lost, which makes every
    other observation in the run meaningless, so it blocks instead.
    """
    recorder = Recorder("N1")
    try:
        # The dataset the restricted user is refused has to exist, or the
        # refusal would be about a missing object rather than a permission.
        dataset_id = _dataset_id(_login(target, target.username, target.password))
        client = _login(target, target.restricted_username, target.restricted_password)
        listing = client.get("/api/v1/chart/")
        if listing.status_code == 401:
            raise Blocked("the restricted session is not authenticated (HTTP 401 on list)")
        recorder.check("control_the_restricted_role_can_list_charts", CONTROL, 200, listing.status_code)

        # The same write the portal's restricted profile attempts: saving
        # exploration state on a dataset the Gamma role cannot reach.
        write = client.post(
            f"/api/v1/explore/form_data?tab_id={N1_TAB}",
            json=_payload(dataset_id, _form_data(dataset_id, "region", 100)),
        )
        if write.status_code == 401:
            raise Blocked("the restricted session lost authentication before the write (HTTP 401)")
        if write.status_code >= 500:
            raise Blocked(
                f"the restricted write returned HTTP {write.status_code}: a server "
                "error is not an authorization answer"
            )
        recorder.check(
            "the_restricted_role_is_denied_a_write", TARGET, 403, write.status_code,
            "an authenticated permission denial, not an authentication failure",
        )

        data = client.post(
            "/api/v1/chart/data",
            json={"datasource": {"id": dataset_id, "type": "table"},
                  "queries": [{"row_limit": 1}],
                  "result_format": "json", "result_type": "full"},
        )
        if data.status_code == 401:
            raise Blocked("the restricted session lost authentication before chart data (HTTP 401)")
        recorder.check(
            "the_restricted_role_is_denied_chart_data", TARGET, 403, data.status_code
        )
    except Blocked as exc:
        return _blocked("N1", str(exc), recorder)
    except requests.RequestException as exc:
        return _blocked("N1", f"transport error: {type(exc).__name__}", recorder)
    return recorder.result()


CASES: dict[str, Callable[[Target], CaseResult]] = {
    "S2": case_s2,
    "S1": case_s1,
    "N1": case_n1,
}


def run(cases: tuple[str, ...], target: Target) -> dict[str, Any]:
    """Replay each case and fold the results into one machine-readable verdict.

    MCP readiness is measured before the cases and reported on its own, so a
    report can say whether the MCP service was reachable at all without that
    answer hiding inside an S1 verdict.
    """
    readiness = mcp_readiness(target) if target.mcp_url else {}
    results = [CASES[case](target) for case in cases if case in CASES]
    unknown = [case for case in cases if case not in CASES]
    verdict = PASSED
    if unknown or not results:
        verdict = BLOCKED
    elif any(r.verdict == BLOCKED for r in results):
        verdict = BLOCKED
    elif any(r.verdict == FAILED for r in results):
        verdict = FAILED
    return {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "cases": [result.to_dict() for result in results],
        "mcp_readiness": readiness,
        "unknown_cases": unknown,
        "target": {"base_url": target.base_url, "mcp_url": target.mcp_url},
        "failures": failures_of(results),
    }


def failures_of(results: list[CaseResult]) -> list[str]:
    """Precise expected/observed lines, the only thing feedback may quote."""
    lines: list[str] = []
    for result in results:
        if result.verdict == BLOCKED:
            lines.append(f"{result.case}: blocked — {result.blocked_reason}")
            continue
        for check in result.checks:
            if not check.holds:
                lines.append(
                    f"{result.case}.{check.name}: expected {check.expected!r}, "
                    f"observed {check.observed!r}"
                    + (f" ({check.note})" if check.note else "")
                )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", required=True, choices=sorted(CASES))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--mcp-url", default="")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--restricted-username", default="restricted_analyst")
    parser.add_argument("--restricted-password", default="restricted-analyst-local")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    report = run(
        tuple(args.case),
        Target(
            base_url=args.base_url,
            mcp_url=args.mcp_url,
            username=args.username,
            password=args.password,
            restricted_username=args.restricted_username,
            restricted_password=args.restricted_password,
        ),
    )
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    # A blocked run is not a pass: the exit code says so.
    return {PASSED: 0, FAILED: 1, BLOCKED: 2}[report["verdict"]]


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
