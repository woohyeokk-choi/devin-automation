"""Synthetic Analytics portal (customer surface) + operator log viewer.

Server-rendered HTML, no frontend framework, no telemetry stack: FastAPI +
Jinja + SQLite only. The customer pages talk to the real Superset light stack;
the `/ops` pages are password-protected and expose the structured event log,
per-trace detail and a JSONL export.
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .config import settings
from .domain import DIMENSIONS, SORTS, Denied, ExplorationSpec, FixtureMissing, Portal
from .events import EventStore
from .handoff import FILES as BUNDLE_FILES, write_bundle
from .controller import RepairStore, lifecycle
from .verification import VerificationStore
from .worker import RepairWorker, build_controller
from .incidents import IncidentStore
from .notify import NotificationLog
from .provenance import summary as provenance_summary
from .security import (
    PROFILES,
    apply_session_cookies,
    csrf_guard,
    csrf_token_of,
    demo_guard,
    ops_guard,
    profile_of,
    require_known_profile,
)
from .tracing import Trace
from .upstream import NotAuthenticated, UpstreamUnavailable

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
store = EventStore(settings.db_path)
incidents = IncidentStore(
    settings.db_path.with_name("incidents.sqlite"),
    target_repo=settings.target_repo,
    parent_fingerprint=settings.parent_incident,
    expected_baseline=settings.baseline_sha,
    telemetry_admission=settings.telemetry_admission,
    telemetry_rate_limit=settings.telemetry_rate_limit,
)
# Observation, not dispatch: the incident model is fed from the server's own
# event stream regardless of AUTO_REPAIR_ENABLED.
incidents.event_log = store
# The callback runs after the event is committed. A crash in that window would
# lose the incident, so every start folds anything the callback never saw. The
# replay is free: an already-folded event is a duplicate by event_id.
incidents.drain(store)
portal = Portal(settings, store)


def handoff_versions() -> dict[str, Any]:
    """What a bundle or a brief must pin, all of it measured here."""
    return {
        "portal_run_id": settings.run_id,
        "environment_kind": settings.environment_kind,
        "measured_at": portal.provenance.get("measured_at"),
        "running_code_sha256": portal.provenance.get("source", {}).get(
            "running_code_sha256"
        ),
        "container_image_id": portal.provenance.get("container", {}).get("image_id"),
        "automation_sha": portal.provenance.get("automation", {}).get("checkout_sha"),
        "automation_dirty": portal.provenance.get("automation", {}).get("checkout_dirty"),
    }


repairs = RepairStore(settings.db_path.with_name("repairs.sqlite"))
# Every verification attempt, whatever it concluded. Kept beside the repairs
# so the console can show the evidence timeline even for attempts that were
# blocked, and so a simulated run can never be counted as a real pass.
verifications = VerificationStore(settings.db_path.with_name("verifications.sqlite"))
# Read-only here. The console shows what the coordinator tried to announce and
# what became of it; the webhook itself is never configured in this process,
# so a compromised web application cannot post to the channel.
notifications = NotificationLog(settings.db_path.with_name("notifications.sqlite"))
# With AUTO_REPAIR_ENABLED=false no provider is configured and none is faked:
# the controller records the exact issue and session bodies instead of sending
# them. With it true the live clients are built from the configured
# credentials, and their absence stops the process rather than degrading to a
# simulated success. Observation and proposal never wait for the console.
controller = build_controller(
    repairs,
    target_repo=settings.target_repo,
    versions=handoff_versions(),
    dispatch_enabled=settings.auto_repair_enabled,
    # The verifier is built only when dispatch is on and this deployment was
    # told where to build candidate stacks. It is never faked: a candidate
    # with no verifier is parked for attention, not passed.
    verifications=verifications,
    automation_dir=Path(settings.automation_dir) if settings.automation_dir else None,
    workspace=(
        Path(settings.verification_workspace)
        if settings.verification_workspace
        else None
    ),
    artifacts=(
        Path(settings.verification_artifacts)
        if settings.verification_artifacts
        else None
    ),
    web_port=settings.verification_web_port,
    mcp_port=settings.verification_mcp_port,
    # The worker rebuilds the brief from stored evidence when it claims a
    # queued proposal, rather than carrying it from the request that observed
    # the failure.
    incident_of=lambda incident_id: incidents.get(incident_id),
    run=settings.run_namespace,
)
# Network work belongs off the request path: polling a session, and resolving
# a creation claim whose worker died, happen on this timer.
worker = RepairWorker(controller)


def observe_and_consider(event: dict[str, Any]) -> dict[str, Any]:
    """One failed user action: fold it, then let the controller decide."""
    result = incidents.observe(event)
    consider(str(result.get("fingerprint") or ""))
    return result


def consider(fingerprint: str) -> None:
    """Queue the repair. Nothing here talks to GitHub or Devin.

    `consider` writes a proposal row and returns; the worker claims the
    single-flight slot and does the dispatching, so this customer request
    finishes at local-database speed.
    """
    incident = incidents.by_fingerprint(fingerprint) if fingerprint else None
    if incident is not None:
        controller.consider(incident)


store.observer = observe_and_consider
for pending in incidents.eligible():
    # Catch-up covers the controller too: an incident folded by the drain has
    # never been seen by a live observer.
    controller.consider(pending)
app = FastAPI(title="Synthetic Analytics portal", docs_url=None, redoc_url=None)
app.add_event_handler("startup", worker.start)
app.add_event_handler("shutdown", worker.stop)

# Every route below the demo gate. `/healthz` stays open so a container health
# check needs no credential; nothing else does, because an anonymous client
# must not be able to mutate upstream state or mint events.
GATED = [Depends(demo_guard)]
# State-changing routes additionally prove the request came from this site.
WRITE = [Depends(demo_guard), Depends(csrf_guard)]


# ------------------------------------------------------------------ helpers
def tab_of(request: Request) -> str:
    return request.cookies.get("portal_tab", "100001")


def base_context(request: Request, **extra: Any) -> dict[str, Any]:
    context = {
        "request": request,
        "profile": profile_of(request),
        "profiles": PROFILES,
        "csrf_token": csrf_token_of(request),
        "dimensions": DIMENSIONS,
        "sorts": SORTS,
        "environment_kind": settings.environment_kind,
        "auto_repair_enabled": settings.auto_repair_enabled,
        "telemetry_admission": settings.telemetry_admission,
        "telemetry_rate_limit": settings.telemetry_rate_limit,
        "provenance": provenance_summary(portal.provenance),
        "dataset_table": settings.dataset_table,
    }
    context.update(extra)
    return context


def render(name: str, request: Request, **extra: Any) -> HTMLResponse:
    context = base_context(request, **extra)
    response = TEMPLATES.TemplateResponse(request, name, context)
    apply_session_cookies(response, context["profile"], context["csrf_token"])
    return response


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


@app.get("/", response_class=HTMLResponse, dependencies=GATED)
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
    except (UpstreamUnavailable, NotAuthenticated, FixtureMissing) as exc:
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


@app.post("/profile", dependencies=WRITE)
def switch_profile(request: Request, profile: str = Form(...)) -> RedirectResponse:
    """Switch demo profile by name; an unknown name is refused, not downgraded.

    The name selects a fixed server-side Superset credential. A silent fallback
    to `analyst` would let a typo or a probe pick the *more* capable identity,
    so the request fails instead.
    """
    profile = require_known_profile(profile)
    response = back(request, note=f"demo profile set to {PROFILES[profile]}")
    apply_session_cookies(response, profile, csrf_token_of(request))
    response.set_cookie("portal_tab", tab_of(request))
    return response


@app.post("/tabs/new", dependencies=WRITE)
def start_another_exploration(request: Request) -> RedirectResponse:
    """Open a fresh exploration workspace, as a new browser tab would."""
    trace = portal.new_trace(profile_of(request), scenario="S2")
    tab_id = str(secrets.randbelow(900000) + 100000)
    trace.action("portal.start_another_exploration", output={"tab_id": tab_id})
    response = back(request, trace, note="started a new exploration")
    response.set_cookie("portal_tab", tab_id)
    return response


@app.post("/explorations", dependencies=WRITE)
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
    except (UpstreamUnavailable, NotAuthenticated, FixtureMissing) as exc:
        trace.blocked("portal.save_exploration", exc)
        return back(request, trace, note="blocked", keep=spec.to_dict())
    if key is None:
        return back(
            request, trace, note=f"save refused ({http_status})", keep=spec.to_dict()
        )
    return back(request, trace, note="exploration saved", keep=spec.to_dict())


@app.get("/explorations/{key}", response_class=HTMLResponse, dependencies=GATED)
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
    except (UpstreamUnavailable, NotAuthenticated, FixtureMissing) as exc:
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


@app.post("/explorations/{key}/discard", dependencies=WRITE)
def discard_exploration(request: Request, key: str) -> RedirectResponse:
    profile = profile_of(request)
    trace = portal.new_trace(profile, scenario="S2")
    trace.action("portal.discard_exploration_requested", input={"key": key})
    try:
        gateway = portal.gateway(trace, profile)
        portal.discard_exploration(trace, gateway, key)
    except (UpstreamUnavailable, NotAuthenticated, FixtureMissing) as exc:
        trace.blocked("portal.discard_exploration", exc)
        return back(request, trace, note="blocked")
    return back(request, trace, note="exploration discarded")


@app.get("/settings", response_class=HTMLResponse, dependencies=GATED)
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
    except (UpstreamUnavailable, NotAuthenticated, FixtureMissing) as exc:
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


@app.post("/settings/sort", dependencies=WRITE)
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
    except (UpstreamUnavailable, NotAuthenticated, FixtureMissing) as exc:
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


@app.get("/ops/notifications", response_class=HTMLResponse)
def ops_notifications(request: Request, _: str = Depends(ops_guard)) -> HTMLResponse:
    """The Slack delivery ledger: what was announced, and what failed."""
    return render(
        "ops_notifications.html",
        request,
        notifications=list(reversed(notifications.list())),
        totals=notifications.totals(),
        threads=notifications.threads(),
        uploads=list(reversed(notifications.uploads())),
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


@app.get("/ops/incidents", response_class=HTMLResponse)
def ops_incidents(request: Request, _: str = Depends(ops_guard)) -> HTMLResponse:
    listed = incidents.list()
    return render(
        "ops_incidents.html",
        request,
        incidents=listed,
        lifecycles={
            item["id"]: lifecycle(item, repairs.by_fingerprint(str(item["fingerprint"])))
            for item in listed
        },
        totals=incidents.totals(),
        processing_errors=incidents.processing_errors(),
    )


@app.get("/ops/incidents/{incident_id}", response_class=HTMLResponse)
def ops_incident(
    request: Request, incident_id: int, note: str | None = None, _: str = Depends(ops_guard)
) -> HTMLResponse:
    incident = incidents.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="unknown incident")
    repair = repairs.by_fingerprint(str(incident["fingerprint"]))
    return render(
        "ops_incident.html",
        request,
        incident=incident,
        # The repair row, not the incident row, knows how this ended.
        lifecycle=lifecycle(incident, repair),
        bundle_files=BUNDLE_FILES,
        note=note,
        # What the controller would send, or did: the disabled path is the
        # same path, so the proposal is inspectable before anything is live.
        repair=repair,
        intents=intents_of(str(incident["fingerprint"])),
        # Agent-reported readiness and the independent verdict are rendered
        # from different sources on purpose.
        attempts=[with_checks(a) for a in verifications.for_incident(incident_id)],
        verified_total=verifications.verified_count(),
        auto_repair_enabled=settings.auto_repair_enabled,
    )


def with_checks(attempt: dict[str, Any]) -> dict[str, Any]:
    """An attempt with its stored report opened up into per-case checks.

    A verdict is only evidence if the assertions behind it are readable, so
    the console shows the same names, expected and observed values the
    grader used rather than a summary the reader has to trust.
    """
    try:
        report = json.loads(str(attempt.get("report") or ""))
    except json.JSONDecodeError:
        report = {}
    cases = report.get("cases") if isinstance(report, dict) else None
    return attempt | {
        "case_results": [case for case in cases or [] if isinstance(case, dict)],
        "check_count": sum(
            len(case.get("checks") or [])
            for case in cases or []
            if isinstance(case, dict)
        ),
    }


@app.get("/ops/verifications/{verification_id}/report")
def ops_verification_report(verification_id: int, _: str = Depends(ops_guard)) -> Response:
    """The stored replay record, served from the database.

    The artifact on disk is the same record; reading the row avoids handing a
    path from a table straight to the filesystem.
    """
    attempt = verifications.get(verification_id)
    if attempt is None or not attempt.get("report"):
        raise HTTPException(status_code=404, detail="unknown verification")
    return Response(str(attempt["report"]), media_type="application/json")


def intents_of(fingerprint: str) -> list[dict[str, Any]]:
    repair = repairs.by_fingerprint(fingerprint)
    return repairs.intents(int(repair["id"])) if repair else []


def bundle_dir(incident: dict[str, Any]) -> Path:
    return settings.data_dir / "handoff" / str(incident["fingerprint"])


@app.post("/ops/incidents/{incident_id}/export", dependencies=[Depends(csrf_guard)])
def ops_export_incident(
    request: Request, incident_id: int, _: str = Depends(ops_guard)
) -> RedirectResponse:
    """Write the handoff bundle from stored evidence only."""
    incident = incidents.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="unknown incident")
    manifest = write_bundle(incident, bundle_dir(incident), versions=handoff_versions())
    query = urlencode({"note": f"bundle written ({len(manifest['files']) + 1} files)"})
    return RedirectResponse(f"/ops/incidents/{incident_id}?{query}", status_code=303)


@app.get("/ops/incidents/{incident_id}/files/{name}")
def ops_bundle_file(
    incident_id: int, name: str, _: str = Depends(ops_guard)
) -> StreamingResponse:
    incident = incidents.get(incident_id)
    if incident is None or name not in BUNDLE_FILES:
        raise HTTPException(status_code=404, detail="unknown bundle file")
    path = bundle_dir(incident) / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="bundle not exported yet")
    return StreamingResponse(
        iter([path.read_text()]),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/ops/fixtures/reset", dependencies=[Depends(csrf_guard)])
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
    except (UpstreamUnavailable, NotAuthenticated, FixtureMissing, Denied) as exc:
        trace.blocked("portal.reset_fixture", exc)
    portal.reload_provenance()
    note = "fixture reset" if chart else "fixture reset incomplete (see trace)"
    return back(request, trace, note=note, path="/ops")
