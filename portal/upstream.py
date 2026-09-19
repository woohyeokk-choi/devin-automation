"""Instrumented wrappers around the real Superset REST and MCP clients.

Every upstream call is logged with its own `request_id`, allowlisted input and
output, the real HTTP/tool outcome and a duration. Credentials live here only —
the browser never supplies an upstream identity and no arbitrary URL, tool or
SQL can be reached through the portal.
"""

from __future__ import annotations

from typing import Any, Callable

import requests

from clients.mcp_client import MCPClient, MCPError
from clients.superset_client import SupersetClient

from .config import Settings
from .redaction import pick, safe_exception, scrub
from .tracing import Timer, Trace, new_id


class UpstreamUnavailable(RuntimeError):
    """The upstream service could not be reached or authenticated."""


# Allowlists: only these keys of a request/response body are ever recorded.
REQUEST_FIELDS = frozenset(
    {"datasource_id", "datasource_type", "chart_id", "tab_id", "form_data_summary",
     "key", "identifier", "chart_name", "dataset_id", "config", "queries_len"}
)
RESPONSE_FIELDS = frozenset(
    {"key", "message", "id", "result_summary", "row_count", "columns", "params_summary",
     "chart", "errors", "status"}
)


class SupersetGateway:
    """Server-side Superset session for one portal profile."""

    def __init__(self, settings: Settings, profile: str) -> None:
        self.settings = settings
        self.profile = profile
        self.client = SupersetClient(settings.superset_base_url)
        if profile == "restricted_viewer":
            self.username = settings.restricted_username
            self.password = settings.restricted_password
        else:
            self.username = settings.upstream_username
            self.password = settings.upstream_password

    def login(self, trace: Trace) -> None:
        request_id = new_id("req")
        try:
            with Timer() as timer:
                self.client.login(self.username, self.password)
        except Exception as exc:  # noqa: BLE001 - classified as blocked below
            trace.log(
                "upstream_call",
                "superset.login",
                "blocked",
                request_id=request_id,
                message=safe_exception(exc),
                input={"profile": self.profile},
            )
            raise UpstreamUnavailable(safe_exception(exc)) from exc
        trace.log(
            "upstream_call",
            "superset.login",
            "ok",
            request_id=request_id,
            duration_ms=timer.ms,
            input={"profile": self.profile},
            output={"authenticated": True},
        )

    def call(
        self,
        trace: Trace,
        operation: str,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        input_summary: dict[str, Any] | None = None,
        output_summary: Callable[[Any], dict[str, Any]] | None = None,
        expected_statuses: tuple[int, ...] = (200, 201),
    ) -> tuple[int, Any]:
        """Perform one upstream REST call and log it.

        Returns `(status, body)`. A non-expected status is *recorded*, not
        raised: 403 for the restricted profile is a legitimate outcome, and a
        200 with wrong content is judged by contract assertions, not here.
        """
        request_id = new_id("req")
        try:
            with Timer() as timer:
                response = self.client.request(
                    method, path, json=json_body, params=params
                )
        except requests.RequestException as exc:
            trace.log(
                "upstream_call",
                operation,
                "blocked",
                request_id=request_id,
                message=safe_exception(exc),
                input=pick(input_summary or {}, REQUEST_FIELDS),
            )
            raise UpstreamUnavailable(safe_exception(exc)) from exc

        try:
            body: Any = response.json()
        except ValueError:
            body = {"message": response.text[:200]}

        if response.status_code in (401, 403):
            outcome = "expected_denial" if self.profile == "restricted_viewer" else "error"
        elif response.status_code in expected_statuses:
            outcome = "ok"
        elif response.status_code >= 500:
            outcome = "error"
        else:
            outcome = "error"

        trace.log(
            "upstream_call",
            operation,
            outcome,
            request_id=request_id,
            http_status=response.status_code,
            duration_ms=timer.ms,
            input={
                "method": method,
                "path": scrub(path),
                **pick(input_summary or {}, REQUEST_FIELDS),
            },
            output={
                **pick(body if isinstance(body, dict) else {"result_summary": body},
                       RESPONSE_FIELDS),
                **(output_summary(body) if output_summary and outcome == "ok" else {}),
            },
        )
        return response.status_code, body


class MCPGateway:
    """Server-side MCP session. Only the two chart tools below are reachable."""

    ALLOWED_TOOLS = ("generate_chart", "update_chart")

    def __init__(self, settings: Settings) -> None:
        self.client = MCPClient(settings.mcp_url)
        self._initialized = False

    def call_tool(
        self,
        trace: Trace,
        tool: str,
        arguments: dict[str, Any],
        *,
        input_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if tool not in self.ALLOWED_TOOLS:
            raise ValueError(f"tool {tool!r} is not exposed by the portal")
        request_id = new_id("req")
        try:
            with Timer() as timer:
                if not self._initialized:
                    self.client.initialize()
                    self._initialized = True
                result = self.client.call_tool(
                    "call_tool", {"name": tool, "arguments": {"request": arguments}}
                )
        except (MCPError, requests.RequestException) as exc:
            trace.log(
                "upstream_call",
                f"mcp.{tool}",
                "blocked",
                request_id=request_id,
                tool_name=tool,
                message=safe_exception(exc),
                input=pick(input_summary or {}, REQUEST_FIELDS),
            )
            raise UpstreamUnavailable(safe_exception(exc)) from exc

        # The MCP proxy reports validation failures as `isError: false` with an
        # "Error: ..." body, so the text is inspected as well.
        text = str(result.get("text") or "")
        failed = (
            bool(result.get("isError"))
            or bool(result.get("error"))
            or result.get("success") is False
            or text.startswith("Error")
        )
        trace.log(
            "upstream_call",
            f"mcp.{tool}",
            "error" if failed else "ok",
            request_id=request_id,
            tool_name=tool,
            duration_ms=timer.ms,
            input=pick(input_summary or arguments, REQUEST_FIELDS),
            output={
                "success": result.get("success"),
                "chart_id": (result.get("chart") or {}).get("id"),
                "errors": result.get("error") or None,
                "message": text[:200] or None,
            },
        )
        return result
