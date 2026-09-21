"""SIMULATED providers. Nothing here talks to GitHub or Devin.

These exist so the transport, reconciliation and budget code can be exercised
offline against the same client classes the live path uses. Two rules keep the
simulation from being mistaken for a result:

* every identifier it mints is prefixed `simulated-`, and every simulated
  repair is stored in its own database with `simulated = 1`;
* the controller refuses to enable dispatch without live providers, so an
  absent API key can never be answered by one of these.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar

from .transport import Ambiguous, Refused, Response

SIMULATED = "simulated-"


@dataclass
class Recorded:
    method: str
    url: str
    json: dict[str, Any] | None
    params: dict[str, Any] | None


@dataclass
class FakeTransport:
    """A scripted wire. `handler` sees each request and returns a Response.

    Raising `Ambiguous` or `Refused` from the handler is how the tests produce
    a write whose outcome is unknown.
    """

    handler: Callable[[Recorded], Response]
    calls: list[Recorded] = field(default_factory=list)
    #: Read by anything holding a real credential: a repair driven by this
    #: wire is not real, and must not produce a real side effect anywhere
    #: else either.
    simulated: ClassVar[bool] = True

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Response:
        call = Recorded(method, url, json, params)
        self.calls.append(call)
        assert "Authorization" in headers, "every call must be authenticated"
        return self.handler(call)


@dataclass
class FakeGitHub:
    """An in-memory issues/pulls API for `portal.providers.GitHub`."""

    issues: list[dict[str, Any]] = field(default_factory=list)
    pulls: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: Paths each simulated pull request touches, for change-scope checks.
    pull_files: dict[int, list[str]] = field(default_factory=dict)
    #: Commit id -> tree id and parents, for merged-content identity.
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: "base...head" -> the paths that comparison reports as changed.
    comparisons: dict[str, list[str]] = field(default_factory=dict)
    #: Set to raise on the next write, simulating a timeout after the server
    #: may already have acted.
    fail_create_with: Exception | None = None
    #: When true, a write that raises still lands server-side.
    write_lands: bool = False
    status: int = 0

    def __call__(self, call: Recorded) -> Response:
        if self.status:
            return Response(self.status, {"message": "simulated failure"})
        if call.method == "POST" and call.url.endswith("/issues"):
            number = len(self.issues) + 1
            issue = {
                "number": number,
                "html_url": f"https://github.com/{SIMULATED}repo/issues/{number}",
                "state": "open",
                "body": (call.json or {}).get("body", ""),
                "title": (call.json or {}).get("title", ""),
            }
            if self.fail_create_with is not None:
                error, self.fail_create_with = self.fail_create_with, None
                if self.write_lands:
                    self.issues.append(issue)
                raise error
            self.issues.append(issue)
            return Response(201, issue)
        if call.method == "GET" and call.url.endswith("/issues"):
            return Response(200, {"items": list(self.issues)})
        if call.method == "GET" and call.url.endswith("/files"):
            number = int(call.url.rsplit("/", 2)[1])
            page = int((call.params or {}).get("page", 1))
            names = self.pull_files.get(number, [])
            start = (page - 1) * 100
            window = names[start:start + 100]
            return Response(200, {"items": [{"filename": name} for name in window]})
        if call.method == "GET" and "/compare/" in call.url:
            span = call.url.rsplit("/compare/", 1)[1]
            page = int((call.params or {}).get("page", 1))
            names = self.comparisons.get(span, [])
            window = names[(page - 1) * 100:(page - 1) * 100 + 100]
            return Response(200, {"files": [{"filename": name} for name in window]})
        if call.method == "GET" and "/commits/" in call.url:
            sha = call.url.rsplit("/", 1)[1]
            commit = self.commits.get(sha)
            if commit is None:
                return Response(404, {"message": f"no commit {sha}"})
            return Response(
                200,
                {
                    "sha": sha,
                    "commit": {"tree": {"sha": commit["tree"]}},
                    "parents": [{"sha": parent} for parent in commit["parents"]],
                },
            )
        if call.method == "GET" and "/pulls/" in call.url:
            number = int(call.url.rsplit("/", 1)[1])
            pull = self.pulls.get(number)
            return Response(200, pull) if pull else Response(404, {"message": "not found"})
        return Response(404, {"message": f"unrouted {call.method} {call.url}"})


@dataclass
class FakeDevin:
    """An in-memory Devin v3 sessions API for `portal.providers.Devin`."""

    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: list[tuple[str, str]] = field(default_factory=list)
    terminated: list[str] = field(default_factory=list)
    #: Files a session holds, keyed by session id, as the attachments
    #: endpoint lists them.
    attachments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    fail_create_with: Exception | None = None
    write_lands: bool = False
    status: int = 0
    #: Sessions per list page, so the cursor path is actually walked.
    page_size: int = 100

    def _new(self, request: dict[str, Any]) -> dict[str, Any]:
        session_id = f"devin-{SIMULATED}{len(self.sessions) + 1}"
        return {
            "session_id": session_id,
            "url": f"https://app.devin.ai/sessions/{SIMULATED}{len(self.sessions) + 1}",
            "status": "running",
            "status_detail": "working",
            "acus_consumed": 0.0,
            "pull_requests": [],
            "structured_output": None,
            "tags": list(request.get("tags") or []),
        }

    def finish(
        self,
        session_id: str,
        output: dict[str, Any] | None,
        pr_url: str = "",
        acus: float = 1.0,
    ) -> None:
        session = self.sessions[session_id]
        session.update(
            status="running",
            status_detail="finished",
            acus_consumed=acus,
            structured_output=output,
            pull_requests=[{"pr_url": pr_url, "pr_state": "open"}] if pr_url else [],
        )

    def set_state(self, session_id: str, **values: Any) -> None:
        self.sessions[session_id].update(values)

    def add_attachment(
        self,
        session_id: str,
        name: str,
        *,
        source: str = "devin",
        url: str = "",
        content_type: str = "video/mp4",
    ) -> None:
        """A file the session holds. `source` says who put it there."""
        self.attachments.setdefault(session_id, []).append(
            {
                "attachment_id": f"att-{len(self.attachments.get(session_id, [])) + 1}",
                "name": name,
                "source": source,
                "content_type": content_type,
                "url": url or f"https://storage.invalid/{name}?signature=simulated",
            }
        )

    def __call__(self, call: Recorded) -> Response:
        if self.status:
            return Response(self.status, {"detail": "simulated failure"})
        tail = call.url.split("/v3/organizations/", 1)[1].split("/", 1)[1]
        if call.method == "POST" and tail == "sessions":
            session = self._new(call.json or {})
            if self.fail_create_with is not None:
                error, self.fail_create_with = self.fail_create_with, None
                if self.write_lands:
                    self.sessions[session["session_id"]] = session
                raise error
            self.sessions[session["session_id"]] = session
            return Response(200, session)
        if call.method == "GET" and tail == "sessions":
            params = call.params or {}
            wanted = set(params.get("tags") or [])
            matched = [
                s
                for s in self.sessions.values()
                if not wanted or wanted & set(s.get("tags") or [])
            ]
            start = int(json.loads(params["after"])["offset"]) if params.get("after") else 0
            page = matched[start:start + self.page_size]
            end = start + len(page)
            return Response(
                200,
                {
                    "items": page,
                    "has_next_page": end < len(matched),
                    "end_cursor": json.dumps({"offset": end}) if end < len(matched) else None,
                },
            )
        if call.method == "GET" and tail.endswith("/attachments"):
            session_id = tail.split("sessions/", 1)[1].rsplit("/attachments", 1)[0]
            if session_id not in self.sessions:
                return Response(404, {"detail": "not found"})
            return Response(200, {"items": self.attachments.get(session_id, [])})
        if call.method == "GET":
            session = self.sessions.get(tail.split("sessions/", 1)[1])
            return Response(200, session) if session else Response(404, {"detail": "not found"})
        if call.method == "POST" and tail.endswith("/messages"):
            session_id = tail.split("sessions/", 1)[1].rsplit("/messages", 1)[0]
            if session_id not in self.sessions:
                return Response(404, {"detail": "not found"})
            if self.fail_create_with is not None:
                error, self.fail_create_with = self.fail_create_with, None
                if self.write_lands:
                    self.messages.append((session_id, (call.json or {})["message"]))
                raise error
            self.messages.append((session_id, (call.json or {})["message"]))
            return Response(200, self.sessions[session_id])
        if call.method == "DELETE":
            session_id = tail.split("sessions/", 1)[1]
            self.terminated.append(session_id)
            self.sessions.get(session_id, {}).update(status="exit", status_detail=None)
            return Response(200, {})
        return Response(404, {"detail": f"unrouted {call.method} {call.url}"})


__all__ = ["FakeDevin", "FakeGitHub", "FakeTransport", "Recorded", "SIMULATED", "Ambiguous", "Refused"]
