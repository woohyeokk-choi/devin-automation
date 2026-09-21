"""Portal actions, expressed as things a customer does — not as bug buttons.

Each action drives the real Superset REST/MCP surface, then checks the product
contract the user relies on. A contract violation returned with HTTP 200 is an
`assertion_failed` event; an unreachable service or missing fixture is
`blocked`; the restricted profile's 403 is `expected_denial`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .events import EventStore
from .provenance import load as load_provenance
from .provenance import summary as provenance_summary
from .redaction import SafeError
from .tracing import Trace
from .upstream import MCPGateway, NotAuthenticated, SupersetGateway, UpstreamUnavailable

SERVICE_PROFILE = "portal_service"
RESTRICTED_PROFILE = "restricted_viewer"
DIMENSIONS = ("region", "channel", "product")
SORTS = {"desc": "highest revenue first", "asc": "lowest revenue first"}
#: The saved row limit a deployment uses unless it configures another one.
DEFAULT_ROW_LIMIT = 137
CHART_COLOR_SCHEME = "googleCategory10c"
#: The two shapes the demo chart is ever saved in. An aggregate table answers
#: with one row per region whatever the row limit is, so a deployment that
#: wants the limit to be *visible* in the rendered chart asks for raw records.
AGGREGATE_COLUMNS = [{"name": "region"}, {"name": "revenue", "aggregate": "SUM"}]
RAW_COLUMNS = [
    {"name": "id"},
    {"name": "region"},
    {"name": "channel"},
    {"name": "product"},
    {"name": "revenue"},
]
AGGREGATE_SORT_COLUMN = "SUM(revenue)"
RAW_SORT_COLUMN = "revenue"
REVENUE_METRIC = {
    "expressionType": "SIMPLE",
    "column": {"column_name": "revenue"},
    "aggregate": "SUM",
    "label": "SUM(revenue)",
}


@dataclass
class ExplorationSpec:
    dimension: str = "region"
    sort: str = "desc"
    row_limit: int = 50

    def to_dict(self) -> dict[str, Any]:
        return {"dimension": self.dimension, "sort": self.sort, "row_limit": self.row_limit}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExplorationSpec":
        return cls(
            dimension=data.get("dimension", "region"),
            sort=data.get("sort", "desc"),
            row_limit=int(data.get("row_limit", 50)),
        )

    def label(self) -> str:
        return f"Revenue by {self.dimension} ({SORTS.get(self.sort, self.sort)})"


class FixtureMissing(SafeError):
    """The environment is not set up: `blocked`, never a product verdict."""


class Denied(SafeError):
    """The upstream refused this profile; an expected outcome, not a defect."""

    def __init__(self, status: int, what: str) -> None:
        super().__init__(f"HTTP {status}: this role may not {what}")
        self.status = status

    def safe_detail(self) -> dict[str, Any]:
        return {"denied_status": self.status}


def _sort_of(params: dict[str, Any]) -> tuple[str | None, bool | None]:
    """Read a table chart's persisted sort as `(column, ascending)`.

    Table charts store it in `order_by_cols` as JSON `["column", ascending]`
    pairs; older forms use `timeseries_limit_metric` plus `order_desc`.
    """
    for entry in params.get("order_by_cols") or []:
        try:
            column, ascending = json.loads(entry) if isinstance(entry, str) else entry
        except (ValueError, TypeError):
            continue
        return str(column), bool(ascending)
    metric = params.get("timeseries_limit_metric")
    if metric:
        label = metric.get("label") if isinstance(metric, dict) else metric
        return str(label), not params.get("order_desc", True)
    return None, None


class Portal:
    """Everything the web layer needs; no FastAPI types leak in here."""

    def __init__(self, settings: Settings, store: EventStore) -> None:
        self.settings = settings
        self.store = store
        self.mcp = MCPGateway(settings)
        self._provenance = load_provenance(settings.provenance_path)
        self._dataset_id: int | None = None
        self._service: SupersetGateway | None = None

    # ------------------------------------------------------------ plumbing
    @property
    def provenance(self) -> dict[str, Any]:
        return self._provenance

    def reload_provenance(self) -> None:
        self._provenance = load_provenance(self.settings.provenance_path)

    def new_trace(self, actor: str, scenario: str | None = None) -> Trace:
        # Anything the restricted profile does belongs to the permission
        # control, whichever page it started from.
        if actor == RESTRICTED_PROFILE:
            scenario = "N1"
        return Trace(
            store=self.store,
            actor=actor,
            environment_kind=self.settings.environment_kind,
            run_id=self.settings.run_id,
            revision=provenance_summary(self._provenance),
            scenario=scenario,
        )

    def gateway(self, trace: Trace, profile: str) -> SupersetGateway:
        gateway = SupersetGateway(self.settings, profile)
        gateway.login(trace)
        return gateway

    def service_gateway(self, trace: Trace) -> SupersetGateway:
        """The portal's own identity, used only to resolve fixture ids."""
        if self._service is None:
            self._service = self.gateway(trace, SERVICE_PROFILE)
        return self._service

    def dataset_id(self, trace: Trace, gateway: SupersetGateway) -> int:
        status, body = gateway.call(
            trace, "superset.list_datasets", "GET", "/api/v1/dataset/"
        )
        if status != 200:
            raise FixtureMissing("dataset listing unavailable")
        for item in body.get("result", []):
            if item["table_name"] == self.settings.dataset_table:
                self._dataset_id = int(item["id"])
                return self._dataset_id
        if gateway.profile != SERVICE_PROFILE:
            # A listing scoped by the caller's role can hide a dataset that
            # does exist; the portal owns the fixture mapping, so it resolves
            # the id itself and still issues the data calls as the customer.
            dataset_id = self.dataset_id(trace, self.service_gateway(trace))
            trace.log(
                "note",
                "portal.dataset_not_listed_for_profile",
                "ok",
                message="dataset exists but is not listed for this role",
                output={"dataset_id": dataset_id},
            )
            return dataset_id
        raise FixtureMissing(
            f"dataset {self.settings.dataset_table!r} is not registered — run the seed script"
        )

    # ----------------------------------------------------------- analytics
    def query_rows(
        self, trace: Trace, gateway: SupersetGateway, dataset_id: int, spec: ExplorationSpec
    ) -> list[dict[str, Any]]:
        payload = {
            "datasource": {"id": dataset_id, "type": "table"},
            "result_format": "json",
            "result_type": "results",
            "queries": [
                {
                    "columns": [spec.dimension],
                    "metrics": [REVENUE_METRIC],
                    "orderby": [[REVENUE_METRIC, spec.sort == "asc"]],
                    "row_limit": spec.row_limit,
                }
            ],
        }
        status, body = gateway.call(
            trace,
            "superset.chart_data",
            "POST",
            "/api/v1/chart/data",
            json_body=payload,
            input_summary={"datasource_id": dataset_id, "queries_len": 1},
            output_summary=lambda body: {
                "row_count": len(body["result"][0]["data"]),
                "columns": body["result"][0].get("colnames"),
            },
        )
        if status == 401:
            raise NotAuthenticated(gateway.profile, "superset.query_rows")
        if status == 403:
            raise Denied(status, "query this dataset")
        if status != 200:
            raise FixtureMissing(f"chart data query failed with HTTP {status}")
        return body["result"][0]["data"]

    # -------------------------------------------------------- explorations
    def _form_data(self, dataset_id: int, spec: ExplorationSpec) -> dict[str, Any]:
        return {
            "datasource": f"{dataset_id}__table",
            "viz_type": "table",
            "groupby": [spec.dimension],
            "metrics": [REVENUE_METRIC],
            "order_desc": spec.sort == "desc",
            "row_limit": spec.row_limit,
        }

    def save_exploration(
        self,
        trace: Trace,
        gateway: SupersetGateway,
        dataset_id: int,
        spec: ExplorationSpec,
        tab_id: str,
    ) -> tuple[str | None, int]:
        """Save the current exploration and return its shareable key."""
        body = {
            "datasource_id": dataset_id,
            "datasource_type": "table",
            "form_data": json.dumps(self._form_data(dataset_id, spec)),
        }
        status, response = gateway.call(
            trace,
            "portal.save_exploration",
            "POST",
            f"/api/v1/explore/form_data?tab_id={tab_id}",
            json_body=body,
            input_summary={
                "datasource_id": dataset_id,
                "tab_id": tab_id,
                "form_data_summary": spec.to_dict(),
            },
            expected_statuses=(201,),
        )
        if status == 401:
            # The session is gone: this says nothing about what the role may do.
            raise NotAuthenticated(gateway.profile, "portal.save_exploration")
        if status == 403:
            trace.log(
                "assertion",
                "portal.save_exploration",
                "expected_denial",
                http_status=status,
                message="restricted profile is not permitted to save explorations",
                assertion={
                    "name": "restricted_profile_is_denied",
                    "expected": 403,
                    "observed": status,
                    "holds": status == 403,
                },
            )
            return None, status
        if status != 201:
            trace.blocked(
                "portal.save_exploration",
                message=f"unexpected save status {status}",
            )
            return None, status

        key = response.get("key")
        discarded = {
            item["key"] for item in self.store.explorations() if item["discarded_at"]
        }
        reused = key in discarded
        trace.assert_contract(
            "new_exploration_does_not_reuse_a_discarded_key",
            expected="a key that is not a previously discarded link",
            observed="the key of a discarded exploration" if reused else "a usable key",
            operation="portal.save_exploration",
            holds=not reused,
            known_baseline_defect=True,
            detail=(
                "the link the customer discarded now points at this new exploration"
                if reused
                else None
            ),
        )
        if not reused:
            # Re-saving in the same browser tab updates that tab's state in place,
            # which is normal; the discarded record is kept as evidence instead.
            self.store.save_exploration(
                key, spec.label(), spec.to_dict(), trace.actor, tab_id
            )
        return key, status

    def open_exploration(
        self, trace: Trace, gateway: SupersetGateway, key: str
    ) -> dict[str, Any]:
        """Open a saved exploration link and judge whether it is trustworthy."""
        local = self.store.exploration(key)
        status, body = gateway.call(
            trace,
            "portal.open_exploration",
            "GET",
            f"/api/v1/explore/form_data/{key}",
            input_summary={"key": key},
            expected_statuses=(200, 404),
        )
        upstream_spec: dict[str, Any] | None = None
        if status == 200:
            form_data = json.loads(body["form_data"])
            upstream_spec = {
                "dimension": (form_data.get("groupby") or [None])[0],
                "sort": "desc" if form_data.get("order_desc") else "asc",
                "row_limit": form_data.get("row_limit"),
            }

        if local and local["discarded_at"]:
            trace.assert_contract(
                "discarded_exploration_link_stays_gone",
                expected="HTTP 404 for a discarded exploration",
                observed=f"HTTP {status}",
                operation="portal.open_exploration",
                holds=status == 404,
                known_baseline_defect=True,
                detail=(
                    "the link the customer discarded resolves again and now shows a "
                    "different exploration" if status == 200 else None
                ),
            )
        elif local:
            matches = upstream_spec == local["spec"] if status == 200 else False
            trace.assert_contract(
                "exploration_link_shows_its_own_state",
                expected=local["spec"],
                observed=upstream_spec if status == 200 else f"HTTP {status}",
                operation="portal.open_exploration",
                holds=matches,
                detail=None if matches else "saved link resolves to someone else's state",
            )
        return {"status": status, "spec": upstream_spec, "local": local}

    def discard_exploration(
        self, trace: Trace, gateway: SupersetGateway, key: str
    ) -> bool:
        status, _ = gateway.call(
            trace,
            "portal.discard_exploration",
            "DELETE",
            f"/api/v1/explore/form_data/{key}",
            input_summary={"key": key},
            expected_statuses=(200,),
        )
        if status != 200:
            trace.blocked("portal.discard_exploration", message=f"delete returned {status}")
            return False
        self.store.mark_discarded(key)
        read_status, _ = gateway.call(
            trace,
            "portal.verify_discarded",
            "GET",
            f"/api/v1/explore/form_data/{key}",
            input_summary={"key": key},
            expected_statuses=(404,),
        )
        trace.assert_contract(
            "discard_removes_the_exploration_immediately",
            expected="HTTP 404",
            observed=f"HTTP {read_status}",
            operation="portal.discard_exploration",
            holds=read_status == 404,
        )
        return True

    def _require_editor(
        self, trace: Trace, gateway: SupersetGateway, what: str
    ) -> None:
        """Chart edits run over MCP's own identity, so the portal gates them."""
        if gateway.profile != RESTRICTED_PROFILE:
            return
        trace.log(
            "assertion",
            "portal.chart_edit",
            "expected_denial",
            message=f"portal policy: the restricted profile may not {what}",
            assertion={
                "name": "restricted_profile_cannot_edit_charts",
                "expected": "denied",
                "observed": "denied",
                "holds": True,
            },
        )
        raise Denied(403, what)

    # ------------------------------------------------------- chart settings
    @property
    def raw_rows(self) -> bool:
        """Whether this deployment's chart lists rows instead of aggregating."""
        return self.settings.chart_query_mode == "raw"

    @property
    def sort_column(self) -> str:
        """The column a sort change names, in this chart's shape."""
        return RAW_SORT_COLUMN if self.raw_rows else AGGREGATE_SORT_COLUMN

    def chart_shape(self) -> dict[str, Any]:
        """The columns and query mode every saved state of the chart carries."""
        if self.raw_rows:
            return {
                "chart_type": "table",
                "query_mode": "raw",
                "columns": [dict(column) for column in RAW_COLUMNS],
            }
        return {
            "chart_type": "table",
            "columns": [dict(column) for column in AGGREGATE_COLUMNS],
        }

    def sort_only_request(self, chart_id: int, descending: bool) -> dict[str, Any]:
        """The MCP request a sort change sends — and the one the page shows.

        Built here rather than written out twice, so what the portal displays
        cannot drift from what it sends. There is no `row_limit` key in it.
        """
        config = self.chart_shape()
        config["sort_by"] = [{"column": self.sort_column, "ascending": not descending}]
        return {"identifier": chart_id, "generate_preview": False, "config": config}

    def find_chart(self, trace: Trace, gateway: SupersetGateway) -> dict[str, Any] | None:
        query = (
            "(filters:!((col:slice_name,opr:eq,value:'"
            + self.settings.chart_name
            + "')),page_size:5)"
        )
        status, body = gateway.call(
            trace, "superset.find_chart", "GET", "/api/v1/chart/", params={"q": query}
        )
        if status == 401:
            raise NotAuthenticated(gateway.profile, "superset.find_chart")
        if status == 403:
            raise Denied(status, "browse charts")
        if status != 200 or not body.get("result"):
            return None
        chart_id = body["result"][0]["id"]
        return self.read_chart(trace, gateway, chart_id)

    def read_chart(
        self, trace: Trace, gateway: SupersetGateway, chart_id: int
    ) -> dict[str, Any]:
        status, body = gateway.call(
            trace,
            "superset.read_chart",
            "GET",
            f"/api/v1/chart/{chart_id}",
            input_summary={"chart_id": chart_id},
        )
        if status == 401:
            raise NotAuthenticated(gateway.profile, "superset.read_chart")
        if status == 403:
            raise Denied(status, "open this chart")
        if status != 200:
            raise FixtureMissing(f"chart {chart_id} unreadable (HTTP {status})")
        params = json.loads(body["result"]["params"])
        sort_by, ascending = _sort_of(params)
        return {
            "id": chart_id,
            "name": body["result"]["slice_name"],
            "row_limit": params.get("row_limit"),
            "color_scheme": params.get("color_scheme"),
            "sort_by": sort_by,
            "order_desc": None if ascending is None else not ascending,
            "params": params,
        }

    def ensure_chart(
        self, trace: Trace, gateway: SupersetGateway, dataset_id: int
    ) -> dict[str, Any]:
        existing = self.find_chart(trace, gateway)
        if existing:
            return existing
        self._require_editor(trace, gateway, "see or change these chart settings")
        arguments = {
            "dataset_id": dataset_id,
            "chart_name": self.settings.chart_name,
            "save_chart": True,
            "generate_preview": False,
            "config": {
                **self.chart_shape(),
                "row_limit": self.settings.chart_row_limit,
                "color_scheme": CHART_COLOR_SCHEME,
            },
        }
        result = self.mcp.call_tool(
            trace,
            "generate_chart",
            arguments,
            input_summary={"dataset_id": dataset_id, "chart_name": self.settings.chart_name},
        )
        chart_id = (result.get("chart") or {}).get("id")
        if not chart_id:
            raise UpstreamUnavailable("mcp", "generate_chart", "no chart returned")
        return self.read_chart(trace, gateway, int(chart_id))

    def restore_fixture(
        self, trace: Trace, gateway: SupersetGateway, dataset_id: int
    ) -> dict[str, Any]:
        """Put the demo chart back to its known state before a replay.

        Only this fixture chart is touched, through the same supported explicit
        update a customer would make: row limit, sort and colour palette back to
        the documented starting point. A replay that begins here always observes
        the same "before" values, so a reproduction cannot silently depend on
        leftovers from an earlier run.
        """
        chart = self.ensure_chart(trace, gateway, dataset_id)
        self.mcp.call_tool(
            trace,
            "update_chart",
            {
                "identifier": chart["id"],
                "generate_preview": False,
                "config": {
                    **self.chart_shape(),
                    "sort_by": [{"column": self.sort_column, "ascending": False}],
                    "row_limit": self.settings.chart_row_limit,
                    "color_scheme": CHART_COLOR_SCHEME,
                },
            },
            input_summary={"chart_id": chart["id"], "reset_to": "documented baseline"},
        )
        after = self.read_chart(trace, gateway, chart["id"])
        expected = {
            "row_limit": self.settings.chart_row_limit,
            "color_scheme": CHART_COLOR_SCHEME,
            "order_desc": True,
        }
        observed = {key: after[key] for key in expected}
        trace.assert_contract(
            "fixture_reset_reaches_the_documented_starting_state",
            expected=expected,
            observed=observed,
            operation="portal.reset_fixture",
            subject="harness",
            detail=(
                None
                if expected == observed
                else "the replay precondition was not established"
            ),
        )
        return after

    def change_chart_sort(
        self,
        trace: Trace,
        gateway: SupersetGateway,
        chart: dict[str, Any],
        descending: bool,
        explicit_row_limit: int | None = None,
    ) -> dict[str, Any]:
        """Change only the sort order and check that nothing else moved."""
        self._require_editor(trace, gateway, "change chart settings")
        request = self.sort_only_request(chart["id"], descending)
        config: dict[str, Any] = request["config"]
        if explicit_row_limit is not None:
            config["row_limit"] = explicit_row_limit
        self.mcp.call_tool(
            trace,
            "update_chart",
            request,
            input_summary={"chart_id": chart["id"], "config": config},
        )
        after = self.read_chart(trace, gateway, chart["id"])

        requested = {"sort_by": self.sort_column, "descending": descending}
        observed = {"sort_by": after["sort_by"], "descending": after["order_desc"]}
        sort_applied = bool(after["sort_by"]) and after["order_desc"] == descending
        trace.assert_contract(
            "requested_sort_change_took_effect",
            expected=requested,
            observed=observed,
            operation="portal.change_chart_sort",
            holds=sort_applied,
            detail=None if sort_applied else "the requested sort was not persisted",
        )
        if explicit_row_limit is None:
            trace.assert_contract(
                "row_limit_survives_an_unrelated_change",
                expected=chart["row_limit"],
                observed=after["row_limit"],
                operation="portal.change_chart_sort",
                known_baseline_defect=True,
                detail=(
                    None
                    if chart["row_limit"] == after["row_limit"]
                    else "the customer's saved row limit was replaced by the schema default"
                ),
            )
        else:
            trace.assert_contract(
                "explicit_row_limit_is_applied",
                expected=explicit_row_limit,
                observed=after["row_limit"],
                operation="portal.change_chart_sort",
            )
        trace.assert_contract(
            "color_scheme_survives_an_unrelated_change",
            expected=chart["color_scheme"],
            observed=after["color_scheme"],
            operation="portal.change_chart_sort",
            detail="control: this field is expected to be preserved",
        )
        return after
