"""What is actually running, measured per service.

Provenance has to survive the question "prove the PR code ran". A JSON file
that names a SHA does not: it can be stale, it can describe a different
checkout, and a phrase like "strong" is not a measurement. So each service is
measured on its own, from the container that serves it:

* the container and image ids Docker reports for that service;
* the host path it bind-mounts as its source tree, and the commit and clean
  state of *that* path;
* the configuration file it was started with;
* a content digest of the fixture data the run will read, taken through the
  product's own query API rather than from the seed recipe.

`web` and `mcp` are measured separately because they are separate containers
and can disagree — which is exactly the failure this is here to catch.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

from clients.superset_client import SupersetClient

from .events import utcnow

SOURCE_MOUNT = "/app/superset"
CONFIG_ENV = "SUPERSET_CONFIG_PATH"

#: Hashes the Python sources of a tree. Run unchanged on the host checkout and
#: inside each container, so the two digests are comparable: if a container
#: serves anything other than the checked-out commit — an older image layer, a
#: mount that silently did not apply, an edited file — the digests differ.
_DIGEST_SCRIPT = """
import hashlib, os, sys
root = sys.argv[1]
digest = hashlib.sha256()
for base, dirs, files in os.walk(root):
    dirs[:] = sorted(d for d in dirs if d != '__pycache__')
    for name in sorted(files):
        if not name.endswith('.py'):
            continue
        path = os.path.join(base, name)
        digest.update(os.path.relpath(path, root).encode())
        with open(path, 'rb') as handle:
            digest.update(hashlib.sha256(handle.read()).digest())
print(digest.hexdigest()[:32])
"""


def _run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return result.stdout.strip()


def _git(checkout: str, *args: str) -> str:
    return _run("git", "-C", checkout, *args)


def container_id(project: str, service: str) -> str:
    """The container Compose actually created for this project's service."""
    found = _run(
        "docker", "ps", "--all", "--quiet",
        "--filter", f"label=com.docker.compose.project={project}",
        "--filter", f"label=com.docker.compose.service={service}",
    ).splitlines()
    return found[0] if found else ""


def inspect(container: str) -> dict[str, Any]:
    raw = _run("docker", "inspect", container)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data[0] if data else {}


def service_identity(project: str, service: str) -> dict[str, Any]:
    """Container identity plus the host tree it is really running."""
    container = container_id(project, service)
    if not container:
        return {"service": service, "error": "no container for this compose service"}
    info = inspect(container)
    mounts = {m.get("Destination"): m.get("Source") for m in info.get("Mounts") or []}
    source = str(mounts.get(SOURCE_MOUNT) or "")
    env = dict(
        entry.split("=", 1)
        for entry in (info.get("Config") or {}).get("Env") or []
        if "=" in entry
    )
    identity: dict[str, Any] = {
        "service": service,
        "container_id": container,
        "image_id": str(info.get("Image") or ""),
        "state": str((info.get("State") or {}).get("Status") or ""),
        "health": str(((info.get("State") or {}).get("Health") or {}).get("Status") or "none"),
        "source_mount": source,
        "config_path": env.get(CONFIG_ENV, ""),
    }
    identity["code_hash"] = container_tree_digest(container)
    if source and Path(source).exists():
        identity["source_sha"] = _git(source, "rev-parse", "HEAD")
        identity["clean"] = not _git(source, "status", "--porcelain")
    else:
        identity["source_sha"] = ""
        identity["clean"] = None
        identity["note"] = "the source mount is not a path on this host"
    return identity


def tree_digest(root: Path) -> str:
    """The digest of a source tree on this host."""
    if not root.exists():
        return ""
    return _run(sys.executable, "-c", _DIGEST_SCRIPT, str(root))


def container_tree_digest(container: str, path: str = SOURCE_MOUNT) -> str:
    """The digest of the source tree as seen from inside the container."""
    if not container:
        return ""
    lines = _run(
        "docker", "exec", container, "python3", "-c", _DIGEST_SCRIPT, path
    ).splitlines()
    return lines[-1].strip() if lines else ""


def config_revision(paths: list[Path]) -> str:
    """A digest of the configuration files this deployment supplies."""
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode())
        digest.update(path.read_bytes() if path.exists() else b"<missing>")
    return digest.hexdigest()[:16]


def fixture_digest(
    base_url: str, dataset_table: str, username: str, password: str
) -> dict[str, Any]:
    """A digest of the fixture rows as the product itself returns them.

    Read back through `/api/v1/chart/data`, not from the seed script: the
    question is what the running stack holds, and a recipe cannot answer it.
    """
    try:
        client = SupersetClient(base_url)
        client.login(username, password)
        dataset_id = client.find_dataset(dataset_table)
        if dataset_id is None:
            return {"kind": "content", "error": f"{dataset_table} is not present"}
        response = client.post(
            "/api/v1/chart/data",
            json={
                "datasource": {"id": int(dataset_id), "type": "table"},
                "queries": [
                    {
                        "columns": ["region", "channel", "product"],
                        "metrics": [
                            {
                                "expressionType": "SIMPLE",
                                "column": {"column_name": "revenue"},
                                "aggregate": "SUM",
                                "label": "SUM(revenue)",
                            }
                        ],
                        "row_limit": 10000,
                    }
                ],
                "result_format": "json",
                "result_type": "full",
            },
        )
        if response.status_code != 200:
            return {"kind": "content", "error": f"query returned HTTP {response.status_code}"}
        rows = response.json()["result"][0]["data"]
    except (requests.RequestException, RuntimeError, KeyError, ValueError, IndexError) as exc:
        return {"kind": "content", "error": f"{type(exc).__name__}"}
    material = json.dumps(sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str)),
                          sort_keys=True, default=str)
    return {
        "kind": "content",
        "rows": len(rows),
        "digest": hashlib.sha256(material.encode()).hexdigest()[:32],
        "description": "sha256 of the fixture rows read back through the chart-data API",
    }


def measure(
    *,
    project: str,
    web_service: str,
    mcp_service: str,
    base_url: str,
    dataset_table: str,
    username: str,
    password: str,
    automation_ref: str,
    checkout: Path | None = None,
    config_files: list[Path] | None = None,
) -> dict[str, Any]:
    """One provenance document, measured per service at this moment.

    `checkout` is the trusted tree the verifier itself checked out. Its digest
    is what each container's digest has to equal; without it there is nothing
    to compare a container against.
    """
    source = (checkout / "superset") if checkout else None
    return {
        "measured_at": utcnow(),
        "automation_ref": automation_ref,
        "compose_project": project,
        "checkout": {
            "path": str(checkout.resolve()) if checkout else "",
            "source_sha": _git(str(checkout), "rev-parse", "HEAD") if checkout else "",
            "clean": (not _git(str(checkout), "status", "--porcelain")) if checkout else None,
            "code_hash": tree_digest(source) if source else "",
        },
        "config_revision": config_revision(config_files or []),
        "fixture": fixture_digest(base_url, dataset_table, username, password),
        "web": service_identity(project, web_service),
        "mcp": service_identity(project, mcp_service),
    }


def agrees(measured: dict[str, Any]) -> bool:
    """Whether both services run the same clean commit from the same tree."""
    web, mcp = measured.get("web") or {}, measured.get("mcp") or {}
    return bool(
        web.get("source_sha")
        and web.get("source_sha") == mcp.get("source_sha")
        and web.get("clean")
        and mcp.get("clean")
    )


__all__ = [
    "agrees",
    "config_revision",
    "container_tree_digest",
    "tree_digest",
    "fixture_digest",
    "measure",
    "service_identity",
]
