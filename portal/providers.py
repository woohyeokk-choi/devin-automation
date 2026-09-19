"""Narrow clients for the two external systems the controller talks to.

Only the calls the loop actually needs, against the current published
contracts: GitHub REST for the fork's issues and pull requests, and the Devin
v3 organization API for sessions. Both take a `Transport`, so the offline
tests drive this exact code.

Credentials live in the client and never leave it: no token is written to an
event, a bundle, the console or a stored request copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from .transport import Response, Transport

GITHUB_API = "https://api.github.com"
DEVIN_API = "https://api.devin.ai"
#: v3 credentials are `cog_`-prefixed service-user keys. A legacy `apk_` key
#: authenticates against v1/v2 only and would 401/403 here, and an org *slug*
#: from a browser URL is not the `org-` identifier the path wants.
DEVIN_KEY_PREFIX = "cog_"
DEVIN_ORG_PREFIX = "org-"
DEVIN_SESSION_PREFIX = "devin-"


class NotConfigured(Exception):
    """Live credentials are absent. Never silently answered with a fake."""


@dataclass(frozen=True)
class Issue:
    number: int
    url: str
    state: str
    body: str


@dataclass(frozen=True)
class Session:
    """What the source reports about a session. Not a verification verdict.

    `status='running'` with `status_detail='finished'` means the agent thinks
    it is done — candidate readiness, nothing more.
    """

    session_id: str
    url: str
    status: str
    status_detail: str | None
    acus_consumed: float
    pull_requests: tuple[str, ...]
    structured_output: dict[str, Any]
    tags: tuple[str, ...]

    @property
    def agent_finished(self) -> bool:
        return self.status == "exit" or self.status_detail == "finished"

    @property
    def waiting(self) -> bool:
        return self.status_detail in ("waiting_for_user", "waiting_for_approval")

    @property
    def stopped(self) -> bool:
        """Cannot make progress without intervention."""
        return self.status in ("error", "suspended")


def _session(body: dict[str, Any]) -> Session:
    return Session(
        session_id=str(body.get("session_id", "")),
        url=str(body.get("url", "")),
        status=str(body.get("status", "")),
        status_detail=body.get("status_detail"),
        acus_consumed=float(body.get("acus_consumed") or 0.0),
        pull_requests=tuple(
            str(pr.get("pr_url", "")) for pr in body.get("pull_requests") or []
        ),
        structured_output=dict(body.get("structured_output") or {}),
        tags=tuple(str(tag) for tag in body.get("tags") or []),
    )


@dataclass
class GitHub:
    """Issue create/reuse/read plus the one read the verifier needs: a head SHA."""

    transport: Transport
    token: str
    repo: str
    api: str = GITHUB_API

    def __post_init__(self) -> None:
        if not self.token:
            raise NotConfigured("no GitHub token")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Response:
        return self.transport.request(
            method, f"{self.api}{path}", headers=self._headers(), json=json, params=params
        )

    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue:
        response = self._call(
            "POST",
            f"/repos/{self.repo}/issues",
            json={"title": title, "body": body, "labels": labels},
        )
        if not response.ok:
            raise RuntimeError(f"issue not created ({response.error()})")
        return Issue(
            number=int(response.body["number"]),
            url=str(response.body["html_url"]),
            state=str(response.body.get("state", "open")),
            body=str(response.body.get("body") or ""),
        )

    def find_issue(self, marker: str, labels: list[str]) -> Issue | None:
        """The issue carrying this attempt's marker, if one already exists.

        Reuse and post-ambiguity reconciliation are the same question, so they
        are the same call: was an issue for *this* attempt already opened?
        """
        page = 1
        while page <= 10:
            response = self._call(
                "GET",
                f"/repos/{self.repo}/issues",
                params={
                    "labels": ",".join(labels),
                    "state": "all",
                    "per_page": 100,
                    "page": page,
                },
            )
            if not response.ok:
                raise RuntimeError(f"issues not listed ({response.error()})")
            items = response.body.get("items") or []
            for item in items:
                if item.get("pull_request"):
                    # The issues endpoint also lists pull requests; a PR is
                    # never the issue this attempt is looking for.
                    continue
                if marker in (item.get("body") or ""):
                    return Issue(
                        number=int(item["number"]),
                        url=str(item["html_url"]),
                        state=str(item.get("state", "open")),
                        body=str(item.get("body") or ""),
                    )
            if len(items) < 100:
                return None
            page += 1
        return None

    def pull_request_head(self, number: int) -> dict[str, str]:
        """The PR's real head SHA and base, read from GitHub, not from the agent."""
        response = self._call("GET", f"/repos/{self.repo}/pulls/{number}")
        if not response.ok:
            raise RuntimeError(f"pull request not read ({response.error()})")
        head = response.body.get("head") or {}
        base = response.body.get("base") or {}
        return {
            "head_sha": str(head.get("sha") or ""),
            "head_repo": str((head.get("repo") or {}).get("full_name") or ""),
            "base_ref": str(base.get("ref") or ""),
            "state": str(response.body.get("state") or ""),
            "merged": str(bool(response.body.get("merged"))).lower(),
        }

    def pull_request_files(self, number: int, limit: int = 300) -> list[str]:
        """Every path the PR touches, so scope can be judged before it runs.

        A PR larger than `limit` files is reported as-is and the caller
        rejects it: a repair for one defect does not touch hundreds of files,
        and silently truncating would hide exactly the file that matters.
        """
        paths: list[str] = []
        page = 1
        while page <= 10:
            response = self._call(
                "GET",
                f"/repos/{self.repo}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            )
            if not response.ok:
                raise RuntimeError(f"pull request files not read ({response.error()})")
            items = response.body.get("items") or []
            paths.extend(str(item.get("filename") or "") for item in items)
            if len(items) < 100 or len(paths) > limit:
                break
            page += 1
        return paths


@dataclass
class Devin:
    """Devin v3 sessions: create, get, message, terminate, and find by tag."""

    transport: Transport
    api_key: str
    org_id: str
    api: str = DEVIN_API

    def __post_init__(self) -> None:
        if not self.api_key or not self.org_id:
            raise NotConfigured("no Devin API key or organization id")
        if not self.api_key.startswith(DEVIN_KEY_PREFIX):
            raise NotConfigured(
                "the v3 API needs a cog_ service-user key; apk_ keys are v1/v2 only"
            )
        if not self.org_id.startswith(DEVIN_ORG_PREFIX):
            raise NotConfigured(
                "the v3 API needs the org- identifier, not the workspace slug"
            )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Response:
        url = f"{self.api}/v3/organizations/{self.org_id}{path}"
        return self.transport.request(
            method, url, headers=self._headers(), json=json, params=params
        )

    def create_session(self, request: dict[str, Any]) -> Session:
        response = self._call("POST", "/sessions", json=request)
        if not response.ok:
            raise RuntimeError(f"session not created ({response.error()})")
        return _session(response.body)

    def get_session(self, session_id: str) -> Session:
        response = self._call("GET", f"/sessions/{session_id}")
        if not response.ok:
            raise RuntimeError(f"session not read ({response.error()})")
        return _session(response.body)

    def send_message(self, session_id: str, message: str) -> Session:
        response = self._call(
            "POST", f"/sessions/{session_id}/messages", json={"message": message}
        )
        if not response.ok:
            raise RuntimeError(f"message not delivered ({response.error()})")
        return _session(response.body)

    def terminate_session(self, session_id: str, archive: bool = True) -> None:
        response = self._call(
            "DELETE", f"/sessions/{session_id}", params={"archive": archive}
        )
        if not response.ok:
            raise RuntimeError(f"session not terminated ({response.error()})")

    def sessions_tagged(self, tag: str, first: int = 100) -> Iterator[Session]:
        """Documented cursor pagination: `first`/`after`, `items`/`end_cursor`."""
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"first": first, "tags": [tag]}
            if cursor:
                params["after"] = cursor
            response = self._call("GET", "/sessions", params=params)
            if not response.ok:
                raise RuntimeError(f"sessions not listed ({response.error()})")
            for item in response.body.get("items") or []:
                yield _session(item)
            if not response.body.get("has_next_page"):
                return
            cursor = response.body.get("end_cursor")
            if not cursor:
                return

    def find_tagged(self, tag: str) -> Session | None:
        """The session carrying this exact tag.

        The filter is the server's; the check is ours. Reconciliation after an
        ambiguous create must not adopt whatever session came back first if it
        does not actually carry this attempt's marker.
        """
        for session in self.sessions_tagged(tag):
            if tag in session.tags and session.session_id.startswith(
                DEVIN_SESSION_PREFIX
            ):
                return session
        return None
