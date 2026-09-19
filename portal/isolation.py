"""Running an untrusted candidate commit in a stack of its own.

Candidate code is code a repair session wrote. It is executed, so it gets its
own everything: Compose project, network, ports, Postgres volume and source
checkout. Nothing that could let it reach the baseline environment or the
controller's credentials is passed in:

* the environment handed to `docker compose` is built from scratch, not
  inherited, so no `GITHUB_TOKEN`, `DEVIN_API_KEY` or operator password can
  travel into a container;
* the source bind mounts point at the candidate checkout, never at the
  baseline tree;
* the Docker socket is never mounted;
* only the single non-secret MCP configuration file is mounted, not the stack
  directory that holds `stack/.env` on a developer host.

Everything here can fail, and every failure is a `RunnerError`, which the
verifier turns into `blocked` rather than into a verdict about the product.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from .measure import measure
from .verification import Environment, RunnerError

#: Environment variables that must never reach a candidate container or the
#: Compose invocation that starts one.
SECRET_NAMES = (
    "GITHUB_TOKEN",
    "DEVIN_API_KEY",
    "DEVIN_ORG_ID",
    "PORTAL_OPS_PASSWORD",
    "PORTAL_DEMO_PASSWORD",
    "PORTAL_COOKIE_SECRET",
)

WEB_SERVICE = "superset-light"
MCP_SERVICE = "superset-mcp-light"


def safe_environment(ports: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    """A minimal environment for Compose, built up rather than filtered down.

    Starting from `os.environ` and deleting known secrets is the wrong shape:
    the next credential added to the deployment would be inherited silently.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""),
    }
    env.update(ports)
    env.update(extra)
    leaked = sorted(name for name in SECRET_NAMES if name in env)
    if leaked:
        raise RunnerError(f"refusing to start a candidate stack carrying {leaked}")
    return {name: value for name, value in env.items() if value != ""}


class IsolatedStack:
    """Checks out one commit and brings up a private stack for it."""

    def __init__(
        self,
        *,
        target_repo: str,
        automation_dir: Path,
        workspace: Path,
        automation_ref: str,
        web_port: int = 8288,
        mcp_port: int = 5208,
        timeout_seconds: int = 900,
        git_host: str = "https://github.com",
    ) -> None:
        self.target_repo = target_repo
        self.automation_dir = automation_dir
        self.workspace = workspace
        self.automation_ref = automation_ref
        self.web_port = web_port
        self.mcp_port = mcp_port
        self.timeout_seconds = timeout_seconds
        self.git_host = git_host

    # --- lifecycle ---------------------------------------------------------
    def prepare(self, head_sha: str) -> Environment:
        project = f"candidate{head_sha[:12]}"
        checkout = self.workspace / project
        commands: list[str] = []
        base_url = f"http://127.0.0.1:{self.web_port}"
        mcp_url = f"http://127.0.0.1:{self.mcp_port}/mcp"
        try:
            self._checkout(project, head_sha, checkout, commands)
            self._compose(
                project, checkout, ["up", "-d", WEB_SERVICE, MCP_SERVICE], commands
            )
            self._wait(f"{base_url}/health", commands)
            self._seed(project, base_url, mcp_url, commands)
            provenance = measure(
                project=project,
                web_service=WEB_SERVICE,
                mcp_service=MCP_SERVICE,
                base_url=base_url,
                dataset_table="synthetic_orders",
                username="admin",
                password="admin",
                automation_ref=self.automation_ref,
                checkout=checkout,
                config_files=[self.automation_dir / "stack" / "superset_config_mcp.py"],
            )
        except RunnerError:
            # Only this attempt's namespace and checkout are removed. The
            # baseline environment is a different Compose project and is not
            # touched; leaving the candidate's volumes behind would let the
            # next attempt inherit a half-seeded database.
            self._discard(project, checkout)
            raise
        return Environment(
            project=project,
            base_url=base_url,
            mcp_url=mcp_url,
            checkout=str(checkout),
            head_sha=head_sha,
            provenance=provenance,
            commands=commands,
        )

    def _discard(self, project: str, checkout: Path) -> None:
        if checkout.exists():
            try:
                self._compose(project, checkout, ["down", "-v", "--remove-orphans"], [])
            except RunnerError:
                pass
            self._remove_tree(project, checkout)

    def teardown(self, environment: Environment) -> None:
        checkout = Path(environment.checkout)
        project = environment.project
        if checkout.exists():
            try:
                self._compose(project, checkout, ["down", "-v", "--remove-orphans"], [])
            except RunnerError:
                # A damaged checkout no longer holds the Compose file, and the
                # containers would outlive it. They carry the project label.
                self._remove_by_label(project)
            self._remove_tree(project, checkout)
        else:
            self._remove_by_label(project)

    def _remove_by_label(self, project: str) -> None:
        """Remove what the project labelled as its own.

        Compose needs its file to take a stack down, and a candidate whose
        checkout was damaged no longer has one; the label outlives it.
        """
        label = f"label=com.docker.compose.project={project}"
        env = safe_environment({}, {})
        for kind in ("container", "volume"):
            try:
                listed = self._run(
                    ["docker", kind, "ls", "-q", "--filter", label],
                    Path.cwd(), [], env=env,
                )
                names = [n for n in listed.split() if n]
                if names:
                    self._run(
                        ["docker", kind, "rm", "-f", *names], Path.cwd(), [], env=env
                    )
            except RunnerError:
                continue

    def _remove_tree(self, project: str, checkout: Path) -> None:
        """Delete a checkout the containers also wrote to.

        Python bytecode the container compiled belongs to root, so the host
        user cannot unlink it and a half-deleted tree would be inherited by
        the next attempt on the same commit. What the container created, a
        container removes — with only this checkout's parent mounted.
        """
        shutil.rmtree(checkout, ignore_errors=True)
        if not checkout.exists():
            return
        try:
            self._run(
                [
                    "docker", "run", "--rm",
                    "--user", "0:0",
                    "--entrypoint", "rm",
                    "-v", f"{checkout.parent}:/workspace",
                    f"{project}-superset-light",
                    "-rf", f"/workspace/{checkout.name}",
                ],
                checkout.parent,
                [],
                env=safe_environment({}, {}),
            )
        except RunnerError:
            # The image may not have been built yet; the residue is then the
            # host's own and the retry below removes it.
            pass
        shutil.rmtree(checkout, ignore_errors=True)

    # --- pieces ------------------------------------------------------------
    def _checkout(
        self, project: str, head_sha: str, checkout: Path, commands: list[str]
    ) -> None:
        if len(head_sha) != 40 or any(c not in "0123456789abcdef" for c in head_sha.lower()):
            raise RunnerError("refusing to check out anything but a full commit id")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        self._remove_tree(project, checkout)
        url = f"{self.git_host}/{self.target_repo}.git"
        self._run(["git", "init", "--quiet", str(checkout)], Path.cwd(), commands)
        self._run(["git", "remote", "add", "origin", url], checkout, commands)
        self._run(["git", "fetch", "--depth", "1", "origin", head_sha], checkout, commands)
        self._run(["git", "checkout", "--quiet", "FETCH_HEAD"], checkout, commands)

    def _compose(
        self, project: str, checkout: Path, action: list[str], commands: list[str]
    ) -> None:
        overlay = self.automation_dir / "stack" / "docker-compose.ports.yml"
        argv = [
            "docker", "compose",
            "--project-name", project,
            "-f", "docker-compose-light.yml",
            "-f", str(overlay),
            *action,
        ]
        env = safe_environment(
            {
                "SUPERSET_PORT_HOST": str(self.web_port),
                "SUPERSET_MCP_PORT_HOST": str(self.mcp_port),
                "SUPERSET_PORT_BIND": "127.0.0.1",
                "SUPERSET_LIGHT_IMAGE": f"{project}-superset-light",
            },
            {"SUPERSET_DIR": str(checkout), "AUTOMATION_DIR": str(self.automation_dir)},
        )
        self._run(argv, checkout, commands, env=env, timeout=self.timeout_seconds)

    def _seed(
        self, project: str, base_url: str, mcp_url: str, commands: list[str]
    ) -> None:
        """Seed the candidate's own stack, including the restricted N1 role.

        The Compose project has to travel with the call: the seed derives its
        database and web container names from it, and its guard refuses to run
        when they belong to another namespace — which is what saves the
        baseline stack from being written to by a candidate run.
        """
        env = safe_environment(
            {},
            {
                "SUPERSET_BASE_URL": base_url,
                "SUPERSET_MCP_URL": mcp_url,
                "SUPERSET_USERNAME": "admin",
                "SUPERSET_PASSWORD": "admin",
                "SUPERSET_COMPOSE_PROJECT": project,
                "COMPOSE_PROJECT_NAME": project,
                "SUPERSET_DB_CONTAINER": f"{project}-db-light-1",
                "SUPERSET_WEB_CONTAINER": f"{project}-{WEB_SERVICE}-1",
            },
        )
        self._run(
            ["python3", "scripts/seed_synthetic.py"],
            self.automation_dir,
            commands,
            env=env,
            timeout=600,
        )

    def _wait(self, url: str, commands: list[str]) -> None:
        commands.append(f"wait for {url}")
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            try:
                if requests.get(url, timeout=5).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(5)
        raise RunnerError(f"{url} never became healthy")

    def _run(
        self,
        argv: list[str],
        cwd: Path,
        commands: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> str:
        commands.append(" ".join(argv))
        try:
            result = subprocess.run(
                argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError(f"{argv[0]} failed: {type(exc).__name__}") from None
        if result.returncode != 0:
            raise RunnerError(
                f"{' '.join(argv[:3])} exited {result.returncode}: "
                f"{result.stderr.strip()[-400:] or 'no stderr'}"
            )
        return result.stdout


def replay_through_validator(environment: Environment, cases: tuple[str, ...]) -> dict[str, Any]:
    """Run the pinned validator against a started stack, in this process.

    The validator is imported from the automation revision the controller is
    running, not from the candidate checkout, so the candidate cannot change
    what grades it.
    """
    from .validator import Target, run

    return run(cases, Target(base_url=environment.base_url, mcp_url=environment.mcp_url))


__all__ = [
    "IsolatedStack",
    "MCP_SERVICE",
    "SECRET_NAMES",
    "WEB_SERVICE",
    "replay_through_validator",
    "safe_environment",
]
