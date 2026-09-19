"""Synthetic Analytics portal (customer surface) + operator log viewer.

Server-rendered HTML, no frontend framework, no telemetry stack: FastAPI +
Jinja + SQLite only. The customer pages talk to the real Superset light stack;
the `/ops` pages are password-protected and expose the structured event log,
per-trace detail and a JSONL export.
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .config import settings
from .domain import DIMENSIONS, SORTS, Denied, ExplorationSpec, FixtureMissing, Portal
from .events import EventStore
from .provenance import summary as provenance_summary
from .tracing import Trace
from .upstream import UpstreamUnavailable

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
store = EventStore(settings.db_path)
portal = Portal(settings, store)
app = FastAPI(title="Synthetic Analytics portal", docs_url=None, redoc_url=None)
security = HTTPBasic()

PROFILES = {
    "analyst": "Dana (analyst)",
    "restricted_viewer": "Robin (restricted viewer)",
}


# ------------------------------------------------------------------ helpers
def profile_of(request: Request) -> str:
    value = request.cookies.get("portal_profile", "analyst")
    return value if value in PROFILES else "analyst"


def tab_of(request: Request) -> str:
    return request.cookies.get("portal_tab", "100001")


def ops_guard(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = secrets.compare_digest(credentials.username, settings.ops_username)
    ok_pass = secrets.compare_digest(credentials.password, settings.ops_password)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="operator login required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def base_context(request: Request, **extra: Any) -> dict[str, Any]:
    context = {
        "request": request,
        "profile": profile_of(request),
        "profiles": PROFILES,
        "dimensions": DIMENSIONS,
        "sorts": SORTS,
        "environment_kind": settings.environment_kind,
        "auto_repair_enabled": settings.auto_repair_enabled,
        "provenance": provenance_summary(portal.provenance),
        "dataset_table": settings.dataset_table,
    }
    context.update(extra)
    return context


def render(name: str, request: Request, **extra: Any) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(name, base_context(request, **extra))


def back(request: Request, trace: Trace | None = None, note: str | None = None,
         path: str = "/", keep: dict[str, Any] | None = None) -> RedirectResponse:
    params: dict[str, str] = {
        name: str(value) for name, value in (keep or {}).items()
    }
    if trace:
        params["trace_id"] = trace.trace_id
    if note:
        params["note"] = note
    suffix = ("?" + urlencode(params)) if params else ""
    return RedirectResponse(path + suffix, status_code=303)


# ------------------------------------------------------------- customer UI
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "environment_kind": settings.environment_kind}


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    dimension: str = "region",
    sort: str = "desc",
    row_limit: int = 50,
    trace_id: str | None = None,
    note: str | None = None,
) -> HTMLResponse:
    spec = ExplorationSpec(
        dimension=dimension if dimension in DIMENSIONS else "region",
        sort=sort if sort in SORTS else "desc",
        row_limit=max(1, min(int(row_limit), 500)),
    )
    profile = profile_of(request)
    trace = portal.new_trace(profile)
    trace.action("portal.view_dashboard", input=spec.to_dict())
    rows: list[dict[str, Any]] = []
    blocked_reason: str | None = None
    denied_reason: str | None = None
    try:
        gateway = portal.gateway(trace, profile)
        dataset_id = portal.dataset_id(trace, gateway)
        rows = portal.query_rows(trace, gateway, dataset_id, spec)
    except Denied as exc:
        denied_reason = str(exc)
    except (UpstreamUnavailable, FixtureMissing) as exc:
        trace.blocked("portal.view_dashboard", exc)
        blocked_reason = str(exc)

    return render(
        "index.html",
        request,
        spec=spec,
        rows=rows,
        blocked_reason=blocked_reason,
        denied_reason=denied_reason,
        explorations=store.explorations(),
        trace_id=trace_id or trace.trace_id,
        note=note,
    )


@app.post("/profile")
def switch_profile(request: Request, profile: str = Form(...)) -> RedirectResponse:
    profile = profile if profile in PROFILES else "analyst"
    response = back(request, note=f"profile set to {PROFILES[profile]}")
    response.set_cookie("portal_profile", profile)
    response.set_cookie("portal_tab", tab_of(request))
    return response


@app.post("/tabs/new")
def start_another_exploration(request: Request) -> RedirectResponse:
    """Open a fresh exploration workspace, as a new browser tab would."""
    trace = portal.new_trace(profile_of(request), scenario="S2")
    tab_id = str(secrets.randbelow(900000) + 100000)
    trace.action("portal.start_another_exploration", output={"tab_id": tab_id})
    response = back(request, trace, note="started a new exploration")
    response.set_cookie("portal_tab", tab_id)
    return response


@app.post("/explorations")
def save_exploration(
    request: Request,
    dimension: str = Form("region"),
    sort: str = Form("desc"),
    row_limit: int = Form(50),
) -> RedirectResponse:
    profile = profile_of(request)
    spec = ExplorationSpec(dimension=dimension, sort=sort, row_limit=int(row_limit))
    trace = portal.new_trace(profile, scenario="S2")
    trace.action("portal.save_exploration_requested", input=spec.to_dict())
    try:
        gateway = portal.gateway(trace, profile)
        dataset_id = portal.dataset_id(trace, gateway)
        key, http_status = portal.save_exploration(
            trace, gateway, dataset_id, spec, tab_of(request)
        )
    except Denied:
        return back(request, trace, note="not permitted for this role", keep=spec.to_dict())
    except (UpstreamUnavailable, FixtureMissing) as exc:
        trace.blocked("portal.save_exploration", exc)
        return back(request, trace, note="blocked", keep=spec.to_dict())
    if key is None:
        return back(
            request, trace, note=f"save refused ({http_status})", keep=spec.to_dict()
        )
    return back(request, trace, note="exploration saved", keep=spec.to_dict())


@app.get("/explorations/{key}", response_class=HTMLResponse)
def open_exploration(request: Request, key: str) -> HTMLResponse:
    profile = profile_of(request)
    trace = portal.new_trace(profile, scenario="S2")
    trace.action("portal.open_exploration_requested", input={"key": key})
    rows: list[dict[str, Any]] = []
    outcome: dict[str, Any] = {}
    blocked_reason: str | None = None
    try:
        gateway = portal.gateway(trace, profile)
        outcome = portal.open_exploration(trace, gateway, key)
        if outcome["status"] == 200 and outcome["spec"]:
            dataset_id = portal.dataset_id(trace, gateway)
            rows = portal.query_rows(
                trace, gateway, dataset_id, ExplorationSpec.from_dict(outcome["spec"])
            )
    except Denied:
        pass
    except (UpstreamUnavailable, FixtureMissing) as exc:
        trace.blocked("portal.open_exploration", exc)
        blocked_reason = str(exc)

    return render(
        "exploration.html",
        request,
        key=key,
        outcome=outcome,
        rows=rows,
        blocked_reason=blocked_reason,
        trace_id=trace.trace_id,
    )


@app.post("/explorations/{key}/discard")
def discard_exploration(request: Request, key: str) -> RedirectResponse:
    profile = profile_of(request)
    trace = portal.new_trace(profile, scenario="S2")
    trace.action("portal.discard_exploration_requested", input={"key": key})
    try:
        gateway = portal.gateway(trace, profile)
        portal.discard_exploration(trace, gateway, key)
    except (UpstreamUnavailable, FixtureMissing) as exc:
        trace.blocked("portal.discard_exploration", exc)
        return back(request, trace, note="blocked")
    return back(request, trace, note="exploration discarded")


@app.get("/settings", response_class=HTMLResponse)
def chart_settings(
    request: Request, trace_id: str | None = None, note: str | None = None
) -> HTMLResponse:
    profile = profile_of(request)
    trace = portal.new_trace(profile, scenario="S1")
    trace.action("portal.view_chart_settings")
    chart: dict[str, Any] | None = None
    blocked_reason: str | None = None
    denied_reason: str | None = None
    try:
        gateway = portal.gateway(trace, profile)
        dataset_id = portal.dataset_id(trace, gateway)
        chart = portal.ensure_chart(trace, gateway, dataset_id)
    except Denied as exc:
        denied_reason = str(exc)
    except (UpstreamUnavailable, FixtureMissing) as exc:
        trace.blocked("portal.view_chart_settings", exc)
        blocked_reason = str(exc)
    return render(
        "settings.html",
        request,
        chart=chart,
        blocked_reason=blocked_reason,
        denied_reason=denied_reason,
        trace_id=trace_id or trace.trace_id,
        note=note,
    )


@app.post("/settings/sort")
def change_sort(
    request: Request,
    descending: str = Form("true"),
    explicit_row_limit: str = Form(""),
) -> RedirectResponse:
    profile = profile_of(request)
    trace = portal.new_trace(profile, scenario="S1")
    trace.action(
        "portal.change_chart_sort_requested",
        input={"descending": descending, "explicit_row_limit": explicit_row_limit},
    )
    try:
        gateway = portal.gateway(trace, profile)
        dataset_id = portal.dataset_id(trace, gateway)
        chart = portal.ensure_chart(trace, gateway, dataset_id)
        portal.change_chart_sort(
            trace,
            gateway,
            chart,
            descending == "true",
            int(explicit_row_limit) if explicit_row_limit.strip() else None,
        )
    except Denied:
        return back(request, trace, note="not permitted for this role", path="/settings")
    except (UpstreamUnavailable, FixtureMissing) as exc:
        trace.blocked("portal.change_chart_sort", exc)
        return back(request, trace, note="blocked", path="/settings")
    return back(request, trace, note="sort updated", path="/settings")


# ------------------------------------------------------------- operator UI
@app.get("/ops", response_class=HTMLResponse)
def ops_events(
    request: Request,
    outcome: str | None = None,
    note: str | None = None,
    trace_id: str | None = None,
    _: str = Depends(ops_guard),
) -> HTMLResponse:
    return render(
        "ops_events.html",
        request,
        events=store.recent(150, outcome or None),
        traces=store.traces(25),
        outcome=outcome or "",
        note=note,
        trace_id=trace_id,
    )


@app.get("/ops/traces/{trace_id}", response_class=HTMLResponse)
def ops_trace(
    request: Request, trace_id: str, _: str = Depends(ops_guard)
) -> HTMLResponse:
    events = store.trace(trace_id)
    if not events:
        raise HTTPException(status_code=404, detail="unknown trace")
    return render("ops_trace.html", request, trace_id=trace_id, events=events)


@app.get("/ops/export.jsonl")
def ops_export(trace_id: str | None = None, _: str = Depends(ops_guard)) -> StreamingResponse:
    filename = f"events{'-' + trace_id if trace_id else ''}.redacted.jsonl"
    return StreamingResponse(
        store.export_jsonl(trace_id),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/ops/fixtures/reset")
def ops_reset(request: Request, _: str = Depends(ops_guard)) -> RedirectResponse:
    """Restore the disposable fixture to its documented starting state.

    Forgets the portal's own exploration records and puts the demo chart back to
    row limit 137, highest-revenue-first and googleCategory10c, so a replay
    always starts from the same "before" values. Only these disposable fixture
    records are touched; nothing else in Superset is deleted from here.
    """
    trace = portal.new_trace("operator")
    removed = store.clear_explorations()
    trace.action("portal.reset_fixture_records", output={"removed": removed})
    chart: dict[str, Any] | None = None
    try:
        gateway = portal.gateway(trace, "analyst")
        chart = portal.restore_fixture(trace, gateway, portal.dataset_id(trace, gateway))
    except (UpstreamUnavailable, FixtureMissing, Denied) as exc:
        trace.blocked("portal.reset_fixture", exc)
    portal.reload_provenance()
    note = "fixture reset" if chart else "fixture reset incomplete (see trace)"
    return back(request, trace, note=note, path="/ops")
