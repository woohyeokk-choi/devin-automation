"""Handoff bundle: what a repair session needs to rebuild and reproduce.

Four plain files rather than an archive, so an operator can read them in the
browser and a later controller can attach them without unpacking anything.
Everything is built from stored, already-sanitized evidence — the bundle adds
no new access to the upstream and no raw payloads.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .events import utcnow
from .incidents import FAMILIES

FILES = ("incident.json", "events.redacted.jsonl", "reproduction.md", "manifest.json")
AUTOMATION_REPO = "woohyeokk-choi/devin-automation"

#: Context a reader of the evidence would otherwise have to guess at.
ANTECEDENT = {
    "S2": (
        "\nThe failing action is the *second* save. The save, read, discard and\n"
        "404 that set it up are separate user actions with their own traces, so\n"
        "they appear in this bundle only when they are part of a trace that\n"
        "failed. Replay the scenario steps above to produce the full antecedent\n"
        "sequence against a fresh workspace."
    ),
}

STEPS = {
    "S2": """1. Sign in to the portal with the demo credentials and stay in the
   same workspace for the whole scenario. The defect is about one workspace's
   own links, so pressing "Start another exploration" (which rotates the
   workspace) is a negative control, not a reproduction.
2. On the dashboard, save an exploration (any dimension/sort/row limit) and
   copy the link it produces.
3. Open that link once; it shows the exploration you saved.
4. Discard the exploration from the dashboard list.
5. Open the link again: it is gone (HTTP 404). This is the correct behaviour
   and the last correct step.
6. Save a *different* exploration in the same workspace.
7. Open the original link again. Expected: still gone. Observed at baseline:
   it resolves and shows the new exploration's state.""",
    "S1": """1. Sign in to the portal with the demo credentials.
2. Reset the fixture (operator console → "Reset demo fixture") so the chart
   starts at row limit 137, highest revenue first, googleCategory10c.
3. Open "Chart settings" and confirm that starting state is read back.
4. Change only the sort direction. Do not touch the row limit; the portal
   omits it from the MCP update exactly as the customer's request does.
5. Read the settings back. Expected: row limit still 137. Observed at
   baseline: it has been reset to 1000. The colour scheme is preserved,
   which is the control that keeps this narrow.""",
}

SETUP = """```bash
# 0. Host prerequisites: Docker with the compose plugin, git, Python 3.11+.
python3 -m venv .venv && . .venv/bin/activate

# 1. The product, at the SHA this incident was observed on. Use a separate
#    checkout: the light stack bind-mounts this directory, so verifying another
#    commit means pointing SUPERSET_DIR at that commit, not rebuilding over it.
git clone https://github.com/{repo}.git superset
git -C superset checkout {sha}

# 2. The automation repo, pinned to the commit that produced this bundle.
#    {automation_note}
git clone https://github.com/{automation_repo}.git devin-automation
cd devin-automation
git checkout {automation_sha}
pip install -r requirements.txt            # host scripts (seed, replay)

# 3. Configuration. Edit the copy, do not source the example: SUPERSET_DIR and
#    AUTOMATION_DIR are absolute paths on *this* host. COMPOSE_PROJECT_NAME
#    keeps a verification run from colliding with an existing baseline stack.
cp stack/.env.example stack/.env
$EDITOR stack/.env
set -a; . stack/.env; set +a
export COMPOSE_PROJECT_NAME=superset      # use e.g. `verify` for an isolated run

# 4. Superset + MCP sidecar, published on loopback only
(cd "$SUPERSET_DIR" && docker compose -p "$COMPOSE_PROJECT_NAME" \\
   -f docker-compose-light.yml \\
   -f "$AUTOMATION_DIR/stack/docker-compose.ports.yml" \\
   up -d superset-light superset-mcp-light)

# 5. Deterministic synthetic fixture (600 rows, revision {fixture}).
#    These scripts run on the HOST, so they use the loopback URLs from
#    stack/.env; the container service names are only reachable inside the
#    Compose network and are passed to the portal as SUPERSET_CONTAINER_*.
python3 scripts/seed_synthetic.py --reset

# 6. Measured provenance for the running containers, then the portal
python3 scripts/capture_provenance.py
docker compose -f stack/docker-compose.portal.yml up -d --build
curl -fs http://127.0.0.1:8090/healthz

# 7. Replay every scenario headlessly instead of clicking (same assertions).
#    Non-zero exit means the replay itself is unhealthy, not that the product
#    behaved: known baseline defects are reported separately.
python3 scripts/export_examples.py --out artifacts/<run-id>/examples
```"""


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle_events(incident: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Full stored traces behind the incident, plus what could not be found.

    The incident itself only holds the events that failed an assertion. A
    repair session needs the requests around them — the save, the delete, the
    404 — so the whole trace is pulled from the event log and the gap is
    stated when a trace is no longer there.
    """
    evidence = {e["event_id"]: e for e in incident["events"] if e.get("event_id")}
    full: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    for trace in incident.get("traces", []):
        steps = (incident.get("trace_events") or {}).get(trace["trace_id"]) or []
        if not steps:
            gaps.append(
                f"trace `{trace['trace_id']}`: only the failing assertions are "
                "retained; the full request sequence is no longer in the event log"
            )
            continue
        for step in steps:
            full[step["event_id"]] = step
    merged = {**evidence, **full}
    ordered = sorted(
        merged.values(),
        key=lambda e: (e.get("ts_utc", ""), e.get("trace_id", ""), e.get("step_index", 0)),
    )
    return ordered, gaps


def reproduction_markdown(
    incident: dict[str, Any],
    events: list[dict[str, Any]],
    gaps: list[str],
    versions: dict[str, Any],
) -> str:
    family = next((f for f in FAMILIES if f.key == incident["family"]), None)
    assertions = sorted(
        {
            (e.get("assertion") or {}).get("name", "")
            for e in incident["events"]
            if e.get("assertion")
        }
        - {""}
    )
    observed = [
        f"- `{(e['assertion'])['name']}`: expected "
        f"`{json.dumps((e['assertion']).get('expected'))}`, observed "
        f"`{json.dumps((e['assertion']).get('observed'))}`"
        for e in incident["events"]
        if e.get("assertion") and not (e["assertion"]).get("holds")
    ]
    automation = automation_pin(versions)
    return f"""# {incident['title']}

Scenario **{incident['scenario']}** · family `{incident['family']}` ·
fingerprint `{incident['fingerprint']}`

{family.statement if family else ''}

## Environment

| | |
|---|---|
| Target repository | `{incident['target_repo']}` |
| Baseline SHA | `{incident['baseline_sha']}` (provenance: {incident['revision_strength']}) |
| Automation source | `{AUTOMATION_REPO}` @ `{automation['sha']}` |
| Fixture revision | `{incident['fixture_revision']}` |
| Actor profile | `{incident['actor']}` (fixed server-side Superset identity) |
| Failed user actions | {incident['occurrence_count']} |
| Evidence events | {incident['event_count']} |
| First seen | {incident['first_seen_at']} |
| Last seen | {incident['last_seen_at']} |

## Rebuild the environment

{SETUP.format(
    repo=incident['target_repo'],
    sha=incident['baseline_sha'],
    fixture=incident['fixture_revision'],
    automation_repo=AUTOMATION_REPO,
    automation_sha=automation['sha'],
    automation_note=automation['note'],
)}

## Reproduce the user action

{STEPS.get(incident['scenario'], 'No scripted steps are registered for this scenario.')}

## Contract assertions recorded

{chr(10).join(f'- `{name}`' for name in assertions) or '- none'}

### Violations

{chr(10).join(dict.fromkeys(observed)) or '- none'}

## Evidence

`events.redacted.jsonl` holds {len(events)} events: the complete stored trace of
every failed user action behind this incident — each upstream REST/MCP request
and its outcome, not only the assertions — ordered by trace and request step.
Allowlisted fields only, scrubbed by the same code that writes the log: no
credentials, cookies, tokens, raw headers or raw bodies.

### Gaps

{chr(10).join(f'- {gap}' for gap in gaps) or '- none: every trace behind this incident is included in full.'}
{ANTECEDENT.get(incident['scenario'], '')}
"""


def automation_pin(versions: dict[str, Any]) -> dict[str, str]:
    """The automation commit to check out, and how trustworthy that pin is."""
    sha = str(versions.get("automation_sha") or "")
    dirty = bool(versions.get("automation_dirty"))
    if not sha:
        return {
            "sha": "main",
            "note": (
                "WARNING: the automation commit was not measured. `main` may not "
                "contain the code that produced this bundle."
            ),
        }
    if dirty:
        return {
            "sha": sha,
            "note": (
                f"WARNING: the automation checkout had uncommitted changes when this "
                f"bundle was written, so {sha} is the nearest commit, not an exact pin."
            ),
        }
    return {"sha": sha, "note": "This is the exact commit that produced this bundle."}


def write_bundle(incident: dict[str, Any], out_dir: Path, versions: dict[str, Any]) -> dict[str, Any]:
    """Write the four files and return the manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    events, gaps = bundle_events(incident)
    body = {k: v for k, v in incident.items() if k not in ("events", "trace_events")}
    body["evidence"] = {
        "events_in_bundle": len(events),
        "failing_assertion_events": len(incident["events"]),
        "gaps": gaps,
    }
    (out_dir / "incident.json").write_text(json.dumps(body, indent=2, default=str) + "\n")
    (out_dir / "events.redacted.jsonl").write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in events)
    )
    (out_dir / "reproduction.md").write_text(
        reproduction_markdown(incident, events, gaps, versions)
    )

    manifest = {
        "generated_at": utcnow(),
        "incident_fingerprint": incident["fingerprint"],
        "target_repo": incident["target_repo"],
        "baseline_sha": incident["baseline_sha"],
        "automation_repo": AUTOMATION_REPO,
        "automation_sha": automation_pin(versions)["sha"],
        "provenance_strength": incident["revision_strength"],
        "fixture_revision": incident["fixture_revision"],
        "evidence_gaps": gaps,
        "versions": versions,
        "files": [
            {
                "name": name,
                "bytes": (out_dir / name).stat().st_size,
                "sha256": sha256_of(out_dir / name),
            }
            for name in FILES
            if name != "manifest.json"
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
