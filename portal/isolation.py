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

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .measure import measure
from .verification import Environment, Portal, RunnerError

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

# superset-frontend/package.json pins node ^24.16.0; the toolchain image has
# to satisfy that engine or npm refuses to install.
NODE_IMAGE = os.environ.get("VERIFIER_NODE_IMAGE", "node:24-bookworm")
WEB_SERVICE = "superset-light"
MCP_SERVICE = "superset-mcp-light"
PORTAL_SERVICE = "portal"

#: Families whose defect is only observable in a browser. Their candidate
#: stacks cost a frontend build and a saved-chart fixture, which the others
#: have no reason to pay.
BROWSER_FAMILIES = ("bigint_number_format_not_applied",)

#: The retained demo's customer profile. It is the image's own development
#: default, on a loopback-only port, holding nothing but synthetic fixtures,
#: and the verifier signs in with it to prove the chart reads back.
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo-local"


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


def free_port(preferred: int) -> int:
    """`preferred` when the host will bind it, another free port otherwise.

    Candidate stacks publish on the loopback interface, and an earlier stack
    that is still up — an evidence run, a previous candidate — owns whatever
    it published. Failing the whole verification over that would report a
    blocked product question for a host bookkeeping detail.
    """
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise RunnerError("no loopback port is available for a candidate stack")


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
        portal_port: int = 8390,
        timeout_seconds: int = 900,
        #: A frontend build is minutes of npm, not seconds of Compose.
        build_timeout_seconds: int = 5_400,
        git_host: str = "https://github.com",
    ) -> None:
        self.target_repo = target_repo
        self.automation_dir = automation_dir
        self.workspace = workspace
        self.automation_ref = automation_ref
        self.web_port = web_port
        self.mcp_port = mcp_port
        self.portal_port = portal_port
        self.timeout_seconds = timeout_seconds
        self.build_timeout_seconds = build_timeout_seconds
        self.git_host = git_host

    # --- lifecycle ---------------------------------------------------------
    def prepare(self, head_sha: str, family: str = "") -> Environment:
        project = f"candidate{head_sha[:12]}"
        checkout = self.workspace / project
        commands: list[str] = []
        self.web_port = free_port(self.web_port)
        self.mcp_port = free_port(self.mcp_port)
        base_url = f"http://127.0.0.1:{self.web_port}"
        mcp_url = f"http://127.0.0.1:{self.mcp_port}/mcp"
        browser_case = family in BROWSER_FAMILIES
        fixture: dict[str, int] = {}
        assets: dict[str, Any] = {}
        try:
            self._checkout(project, head_sha, checkout, commands)
            if browser_case:
                # Before the stack starts: the bind mount would otherwise
                # serve an empty asset directory, and a bundle built later
                # is a different thing from the bundle under test.
                assets = self._build_frontend(project, checkout, head_sha, commands)
            self._compose(
                project, checkout, ["up", "-d", WEB_SERVICE, MCP_SERVICE], commands
            )
            self._wait(f"{base_url}/health", commands)
            self._seed(project, base_url, mcp_url, commands)
            if browser_case:
                fixture = self._seed_browser_fixture(project, commands)
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
            if assets:
                provenance["assets"] = assets
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
            fixture=fixture,
        )

    # --- browser candidates ------------------------------------------------
    def _build_frontend(
        self, project: str, checkout: Path, head_sha: str, commands: list[str]
    ) -> dict[str, Any]:
        """Build the candidate's own frontend bundle, and prove it is its own.

        A frontend defect cannot be verified against a bundle somebody else
        compiled: the served JavaScript, not the diff, is what a browser
        runs. The checkout's asset directory is git-ignored and therefore
        empty, so it is built here from this commit's sources, and the
        measurement below is what later refuses a bundle that did not come
        from this checkout.
        """
        directory = checkout / "superset" / "static" / "assets"
        # The web image ships a Node runtime but no npm and no installed
        # packages, so the build runs in a toolchain image over the same
        # checkout the stack bind-mounts. Webpack writes into
        # superset/static/assets, which is why the whole tree is mounted and
        # not just superset-frontend. `zstd` is a binary dependency of the
        # webpack config (simple-zstd shells out to it) and is absent from
        # the plain Node image. Ownership is handed back at the end so the
        # host can read the bundle and delete the checkout afterwards.
        script = (
            "set -e; apt-get update -qq; apt-get install -y -qq zstd >/dev/null; "
            "npm ci; npm run build; "
            f"chown -R {os.getuid()}:{os.getgid()} /candidate"
        )
        argv = [
            "docker", "run", "--rm",
            "-v", f"{checkout}:/candidate",
            "-w", "/candidate/superset-frontend",
            "-e", "NODE_OPTIONS=--max-old-space-size=8192",
            NODE_IMAGE, "bash", "-lc", script,
        ]
        self._run(
            argv, checkout, commands, env=safe_environment({}, {}),
            timeout=self.build_timeout_seconds,
        )
        built = sorted(p for p in directory.glob("*.js")) if directory.is_dir() else []
        if not built:
            raise RunnerError("the candidate's frontend build produced no bundle")
        digest = hashlib.sha256()
        for path in built:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return {
            "built_from_sha": head_sha,
            "source": str(directory),
            "files": len(built),
            "bundle_hash": f"sha256:{digest.hexdigest()[:16]}",
            "built_at": datetime.now(timezone.utc).isoformat(),
        }

    def _seed_browser_fixture(self, project: str, commands: list[str]) -> dict[str, int]:
        """Create this stack's own saved charts and report their real ids.

        Chart ids are assigned by whichever metadata database the fixture
        ran against, so the builder's ids mean nothing here. The script runs
        inside the candidate's web container, against the candidate's own
        database, and its ids are what the browser case navigates to.
        """
        script = (self.automation_dir / "scenarios" / "b1_fixture.py").read_text()
        argv = [
            "docker", "exec", "-i",
            "-e", "B1_DATABASE=examples",
            f"{project}-{WEB_SERVICE}-1", "python3", "-",
        ]
        commands.append(" ".join(argv) + " < scenarios/b1_fixture.py")
        try:
            result = subprocess.run(
                argv, input=script, capture_output=True, text=True, timeout=600
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError(f"the browser fixture failed: {type(exc).__name__}") from None
        if result.returncode != 0:
            raise RunnerError(
                f"the browser fixture exited {result.returncode}: "
                f"{result.stderr.strip()[-300:] or 'no stderr'}"
            )
        try:
            reported = json.loads(result.stdout.strip().splitlines()[-1])
            ids = {
                "chart_id": int(reported["chart_id"]),
                "control_chart_id": int(reported["control_chart_id"]),
            }
        except (IndexError, KeyError, ValueError) as exc:
            raise RunnerError(f"the browser fixture reported no chart ids: {exc}") from None
        if ids["chart_id"] <= 0 or ids["control_chart_id"] <= 0:
            raise RunnerError("the browser fixture reported unusable chart ids")
        return ids

    def serve_portal(self, environment: Environment) -> Portal:
        """Start the demo portal itself against a retained stack.

        Superset's own port is not the portal: the page the scenario is
        performed on is this project's FastAPI application, and a retained
        backend without it leaves an operator looking at Superset. It joins
        the retained stack's network, so it reaches those containers by
        service name, keeps state of its own so nothing of the live run is
        shared, and dispatches nothing.
        """
        project = environment.project
        port = free_port(self.portal_port)
        data_dir = self.workspace / f"{project}-portal-state"
        data_dir.mkdir(parents=True, exist_ok=True)
        data_dir.chmod(0o700)
        url = f"http://127.0.0.1:{port}"
        commands = environment.commands
        self._measure_into(environment, data_dir, commands)
        self._portal_compose(environment, port, data_dir, ["up", "-d"], commands)
        try:
            self._wait(f"{url}/healthz", commands)
            self._reaches_settings(url, environment.head_sha, commands)
        except RunnerError:
            # Only this service comes down. `--remove-orphans` here would
            # mean "everything in this project the portal file does not
            # describe", which is the retained stack itself.
            self._portal_compose(environment, port, data_dir, ["down"], commands)
            raise
        return Portal(
            url=f"{url}/settings",
            project=project,
            data_dir=str(data_dir),
            cleanup_command=(
                f"PORTAL_DATA_DIR={data_dir} AUTOMATION_DIR={self.automation_dir} "
                f"docker compose --project-name {project} "
                f"-f {self.automation_dir}/stack/docker-compose.portal.yml down"
            ),
        )

    def _measure_into(
        self, environment: Environment, data_dir: Path, commands: list[str]
    ) -> None:
        """Measure this retained stack, into this deployment's own state.

        The portal reports whatever provenance file it is given, so a
        retained demo pointed at the live run's `runtime/provenance.json`
        would truthfully render somebody else's measurement. It gets its
        own, measured from the containers actually serving it, written
        beside its isolated state; the live file is never written here.
        """
        destination = data_dir / "provenance.json"
        env = safe_environment(
            {},
            {
                "AUTOMATION_DIR": str(self.automation_dir),
                "SUPERSET_COMPOSE_PROJECT": environment.project,
                "SUPERSET_BASE_URL": environment.base_url,
            },
        )
        self._run(
            [
                sys.executable,
                str(self.automation_dir / "scripts" / "capture_provenance.py"),
                "--container",
                f"{environment.project}-{WEB_SERVICE}-1",
                "--output",
                str(destination),
            ],
            self.automation_dir,
            commands,
            env=env,
            timeout=self.timeout_seconds,
        )
        try:
            measured = json.loads(destination.read_text())
        except (OSError, ValueError) as exc:
            raise RunnerError(f"the retained portal has no provenance to report: {exc}")
        reported = str((measured.get("source") or {}).get("checkout_sha") or "")
        if reported != environment.head_sha:
            raise RunnerError(
                "the retained portal would report "
                f"{reported[:12] or 'nothing'}, not {environment.head_sha[:12]}"
            )

    def _reaches_settings(self, url: str, head_sha: str, commands: list[str]) -> None:
        """Prove the chart page an operator will show actually works.

        A process answering `/healthz`, or a sign-in prompt on `/settings`,
        says nothing about whether the chart behind it reads back: the page
        has to render the saved chart, through this portal's own upstream
        calls, and name the commit it is serving. Blocked upstreams and a
        stale deployment both surface here rather than in the recording.
        """
        commands.append(f"GET {url}/settings (demo profile)")
        try:
            response = requests.get(
                f"{url}/settings",
                timeout=60,
                allow_redirects=False,
                auth=(DEMO_USERNAME, DEMO_PASSWORD),
            )
        except requests.RequestException as exc:
            raise RunnerError(f"the retained portal did not answer: {type(exc).__name__}")
        if response.status_code != 200:
            raise RunnerError(
                f"the retained portal answered {response.status_code} for /settings"
            )
        page = response.text
        if "Chart unavailable" in page or "Rows shown (row limit)" not in page:
            raise RunnerError("the retained portal served no chart on /settings")
        if head_sha[:12] not in page:
            raise RunnerError(
                f"the retained portal does not report {head_sha[:12]} on its pages"
            )

    def _portal_compose(
        self,
        environment: Environment,
        port: int,
        data_dir: Path,
        action: list[str],
        commands: list[str],
    ) -> None:
        argv = [
            "docker", "compose",
            "--project-name", environment.project,
            "-f", str(self.automation_dir / "stack" / "docker-compose.portal.yml"),
            *action,
        ]
        env = safe_environment(
            {
                "PORTAL_PORT_HOST": str(port),
                "PORTAL_PORT_BIND": "127.0.0.1",
            },
            {
                "PORTAL_DATA_DIR": str(data_dir),
                "AUTOMATION_DIR": str(self.automation_dir),
                # This deployment's own measurement, not the live run's.
                "PORTAL_PROVENANCE_DIR": str(data_dir),
                "PORTAL_PROVENANCE_PATH": "/data/provenance.json",
                "PORTAL_UID": str(os.getuid()),
                "PORTAL_GID": str(os.getgid()),
                "SUPERSET_NETWORK": f"{environment.project}_default",
                "SUPERSET_CONTAINER_BASE_URL": f"http://{WEB_SERVICE}:8088",
                "SUPERSET_CONTAINER_MCP_URL": f"http://{MCP_SERVICE}:5008/mcp",
                "PORTAL_ENVIRONMENT_KIND": "merged-demo",
                "PORTAL_RUN_ID": f"merged-{environment.head_sha[:12]}",
                "PORTAL_BASELINE_SHA": environment.head_sha,
            },
        )
        self._run(
            argv,
            self.automation_dir,
            commands,
            env=env,
            timeout=self.timeout_seconds,
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

    return run(
        cases,
        Target(
            base_url=environment.base_url,
            mcp_url=environment.mcp_url,
            chart_id=environment.fixture.get("chart_id", 0),
            control_chart_id=environment.fixture.get("control_chart_id", 0),
        ),
    )


__all__ = [
    "IsolatedStack",
    "MCP_SERVICE",
    "PORTAL_SERVICE",
    "SECRET_NAMES",
    "WEB_SERVICE",
    "replay_through_validator",
    "safe_environment",
]
