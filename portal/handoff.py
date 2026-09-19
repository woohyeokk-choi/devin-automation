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

_STEPS = {
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
# 1. The product, at the SHA this incident was observed on. Use a separate
#    checkout: the light stack bind-mounts this directory, so verifying another
#    commit means pointing SUPERSET_DIR at that commit, not rebuilding over it.
git clone https://github.com/{repo}.git superset
git -C superset checkout {sha}

# 2. The automation repo (portal, scenarios, fixtures)
git clone https://github.com/woohyeokk-choi/devin-automation.git
cd devin-automation
cp stack/.env.example stack/.env        # set SUPERSET_DIR/AUTOMATION_DIR
set -a; . stack/.env; set +a

# 3. Superset + MCP sidecar, loopback only
(cd "$SUPERSET_DIR" && docker compose -f docker-compose-light.yml \\
   -f "$AUTOMATION_DIR/stack/docker-compose.ports.yml" \\
   up -d superset-light superset-mcp-light)

# 4. Deterministic synthetic fixture (600 rows, revision {fixture})
python3 scripts/seed_synthetic.py

# 5. Measured provenance for the running containers, then the portal
python3 scripts/capture_provenance.py
docker compose -f stack/docker-compose.portal.yml up -d --build
curl -s http://127.0.0.1:8090/healthz

# 6. Replay every scenario headlessly instead of clicking (same assertions)
python3 scripts/export_examples.py --out artifacts/<run-id>/examples
```"""


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reproduction_markdown(incident: dict[str, Any]) -> str:
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
    return f"""# {incident['title']}

Scenario **{incident['scenario']}** · family `{incident['family']}` ·
fingerprint `{incident['fingerprint']}`

{family.statement if family else ''}

## Environment

| | |
|---|---|
| Target repository | `{incident['target_repo']}` |
| Baseline SHA | `{incident['baseline_sha']}` (provenance: {incident['revision_strength']}) |
| Fixture revision | `{incident['fixture_revision']}` |
| Failed user actions | {incident['occurrence_count']} |
| Evidence events | {incident['event_count']} |
| First seen | {incident['first_seen_at']} |
| Last seen | {incident['last_seen_at']} |

## Rebuild the environment

{SETUP.format(repo=incident['target_repo'], sha=incident['baseline_sha'], fixture=incident['fixture_revision'])}

## Reproduce the user action

{_STEPS.get(incident['scenario'], 'No scripted steps are registered for this scenario.')}

## Contract assertions recorded

{chr(10).join(f'- `{name}`' for name in assertions) or '- none'}

### Violations

{chr(10).join(dict.fromkeys(observed)) or '- none'}

## Evidence

`events.redacted.jsonl` holds every event behind this incident, ordered by
trace and request step, sanitized by the same allowlist that writes the log.
No credentials, cookies, tokens or raw bodies are included.
"""


def write_bundle(incident: dict[str, Any], out_dir: Path, versions: dict[str, Any]) -> dict[str, Any]:
    """Write the four files and return the manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in incident.items() if k != "events"}
    (out_dir / "incident.json").write_text(json.dumps(body, indent=2, default=str) + "\n")
    (out_dir / "events.redacted.jsonl").write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in incident["events"])
    )
    (out_dir / "reproduction.md").write_text(reproduction_markdown(incident))

    manifest = {
        "generated_at": utcnow(),
        "incident_fingerprint": incident["fingerprint"],
        "target_repo": incident["target_repo"],
        "baseline_sha": incident["baseline_sha"],
        "provenance_strength": incident["revision_strength"],
        "fixture_revision": incident["fixture_revision"],
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
