#!/usr/bin/env python3
"""Measure what code the Superset container is actually running.

A host-side `git rev-parse HEAD` proves nothing about a running container, so
this script measures the container instead:

* Compose project/service labels, container id/name, image id and mount table
  (`docker inspect`);
* a SHA-256 over the Python tree **inside** the container (`/app/superset`);
* the same hash computed over the host checkout that the mount claims to come
  from, so `checkout_matches_running_code` is an observation rather than an
  assumption;
* the fixture content revision (DDL + row count) from the fixture database.

Anything that could not be measured is reported as `null` with
`strength` downgraded, never filled in with a plausible-looking value.

Usage:
    python3 scripts/capture_provenance.py [--output runtime/provenance.json]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.seed_synthetic import (  # noqa: E402
    COMPOSE_PROJECT,
    DB_CONTAINER,
    DB_NAME,
    DB_USER,
    SCHEMA,
    TABLE_NAME,
    fixture_revision,
)

CONTAINER = os.environ.get(
    "SUPERSET_CONTAINER", f"{COMPOSE_PROJECT}-superset-light-1"
)
AUTOMATION_DIR = Path(
    os.environ.get("AUTOMATION_DIR") or Path(__file__).resolve().parents[1]
)
AUTOMATION_REPO = "woohyeokk-choi/devin-automation"
CODE_DIR_IN_CONTAINER = os.environ.get("SUPERSET_CODE_DIR", "/app/superset")

HASH_SNIPPET = """
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(p for p in root.rglob('*.py') if '__pycache__' not in str(p)):
    digest.update(str(path.relative_to(root)).encode())
    digest.update(path.read_bytes())
print('sha256:' + digest.hexdigest()[:16])
"""


def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)  # type: ignore[arg-type]


def inspect_container(name: str) -> dict[str, object] | None:
    proc = run(["docker", "inspect", name])
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout)[0]
    labels = data["Config"].get("Labels") or {}
    return {
        "name": data["Name"].lstrip("/"),
        "id": data["Id"][:12],
        "image_id": data["Image"][:19],
        "image": data["Config"]["Image"],
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
        "started_at": data["State"].get("StartedAt"),
        "mounts": [
            {
                "type": mount["Type"],
                "source": mount.get("Source"),
                "destination": mount["Destination"],
                "rw": mount.get("RW"),
            }
            for mount in data.get("Mounts", [])
        ],
    }


def hash_in_container(container: str, directory: str) -> str | None:
    proc = run(
        ["docker", "exec", "-i", container, "python", "-c", HASH_SNIPPET, directory]
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def hash_on_host(directory: Path) -> str | None:
    if not directory.is_dir():
        return None
    proc = run([sys.executable, "-c", HASH_SNIPPET, str(directory)])
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def git(path: Path, *args: str) -> str | None:
    proc = run(["git", "-C", str(path), *args])
    return proc.stdout.strip() if proc.returncode == 0 else None


def automation_facts() -> dict[str, object]:
    """Which automation commit produced a handoff, so it can be checked out.

    A bundle that says "clone main" is not reproducible: main may not yet
    contain the code that observed the incident. Uncommitted changes are
    reported rather than hidden, because then no commit is an exact pin.
    """
    return {
        "repo": AUTOMATION_REPO,
        "path": str(AUTOMATION_DIR),
        "checkout_sha": git(AUTOMATION_DIR, "rev-parse", "HEAD"),
        "checkout_branch": git(AUTOMATION_DIR, "rev-parse", "--abbrev-ref", "HEAD"),
        "checkout_dirty": bool(git(AUTOMATION_DIR, "status", "--porcelain")),
        "tree_sha256": hash_on_host(AUTOMATION_DIR / "portal"),
    }


def fixture_facts() -> dict[str, object]:
    proc = run(
        ["docker", "exec", "-i", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME],
        input=f"SELECT count(*) FROM {SCHEMA}.{TABLE_NAME};",  # noqa: S608
    )
    if proc.returncode != 0:
        return {"table": f"{SCHEMA}.{TABLE_NAME}", "rows": None, "revision": None}
    try:
        rows = int(proc.stdout.strip().splitlines()[2].strip())
    except (IndexError, ValueError):
        return {"table": f"{SCHEMA}.{TABLE_NAME}", "rows": None, "revision": None}
    return {
        "table": f"{SCHEMA}.{TABLE_NAME}",
        "rows": rows,
        "revision": fixture_revision(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default=CONTAINER)
    parser.add_argument("--output", default=os.environ.get("PORTAL_DATA_DIR", "runtime") + "/provenance.json")
    args = parser.parse_args()

    container = inspect_container(args.container)
    if container is None:
        payload = {
            "strength": "unmeasured",
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "note": f"container {args.container!r} is not running",
            "automation": automation_facts(),
        }
    else:
        running_hash = hash_in_container(args.container, CODE_DIR_IN_CONTAINER)
        mount = next(
            (
                m
                for m in container["mounts"]  # type: ignore[index]
                if m["destination"] == CODE_DIR_IN_CONTAINER
            ),
            None,
        )
        checkout_path = Path(mount["source"]).parent if mount and mount["source"] else None
        host_hash = hash_on_host(Path(mount["source"])) if mount and mount["source"] else None
        source = {
            "code_dir_in_container": CODE_DIR_IN_CONTAINER,
            "running_code_sha256": running_hash,
            "mount_source": mount["source"] if mount else None,
            "mounted": mount is not None,
            "host_tree_sha256": host_hash,
            "checkout_matches_running_code": (
                bool(running_hash) and running_hash == host_hash
            ),
            "checkout_sha": git(checkout_path, "rev-parse", "HEAD") if checkout_path else None,
            "checkout_describe": (
                git(checkout_path, "describe", "--tags", "--always") if checkout_path else None
            ),
            "checkout_dirty": (
                bool(git(checkout_path, "status", "--porcelain")) if checkout_path else None
            ),
        }
        if running_hash is None:
            strength = "container-identity-only"
        elif mount is None:
            # image-baked code: the hash is of the running tree, with no host claim
            strength = "running-code-hash"
        else:
            strength = "running-code-hash+mount-match"
        payload = {
            "strength": strength,
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "container": container,
            "source": source,
            "fixtures": fixture_facts(),
            "automation": automation_facts(),
            "claim": (
                "Measured: container identity and a hash of the Python tree inside it. "
                "checkout_matches_running_code compares that hash with the host path the "
                "mount points at; it is false or null when the code is baked into the image "
                "or the path is not readable from here."
            ),
        }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
