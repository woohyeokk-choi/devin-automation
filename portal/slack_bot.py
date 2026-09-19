"""Slack Web API delivery for the alert app, one channel and nothing else.

A bot token can post anywhere its scopes reach, so the channel is not a
parameter the caller is trusted with: `APPROVED_CHANNEL` is the only
destination this module will address, and a configured value that disagrees
is a configuration error rather than a second destination to try.

The three outcomes the ledger depends on are the same ones `portal.transport`
defines. Slack's own answer (`ok: false`, `ratelimited`, an HTTP status) is a
*response*: the call reached the API and the message was not posted. Anything
that fails without an answer is `Ambiguous` unless it provably never left the
process, because a retried `chat_postMessage` is a duplicate in a channel
people read, and `files_upload_v2` is three calls whose middle step failing
must not become a second upload.

The SDK's own retries are switched off: retrying belongs to the ledger, which
knows what has already been delivered, and stacking the two would multiply
attempts behind its back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

from .transport import Ambiguous, Refused

#: The only channel this deployment is allowed to write to: the private
#: incident feed the workspace owner approved.
APPROVED_CHANNEL = "C0C3X4BJ97S"

#: Marks these messages as this project's own notifier rather than the
#: official Devin integration, which shares the workspace.
SOURCE_LABEL = "Superset demo automation"


class SlackRejected(Exception):
    """Slack answered and declined. The call arrived; nothing was posted."""


def bot_is_simulated(bot: SlackBot) -> bool:
    """Whether this client is scripted. A client that does not say counts as one."""
    try:
        return bool(bot.simulated)
    except AttributeError:
        return True


def channel_problem(channel: str) -> str:
    """Why this channel cannot be posted to, or an empty string."""
    if channel != APPROVED_CHANNEL:
        return f"only {APPROVED_CHANNEL} is approved for delivery"
    return ""


class SlackBot(Protocol):
    """A Slack Web API client, real or scripted."""

    #: False for a client that reaches Slack, True for a scripted one.
    simulated: bool
    channel: str

    def post(self, text: str, *, thread_ts: str = "") -> str: ...

    def upload(
        self, path: Path, *, title: str, comment: str, thread_ts: str = ""
    ) -> str: ...


@dataclass
class WebClientBot:
    """`slack_sdk.WebClient` bound to one channel, with no retries of its own."""

    token: str
    channel: str = APPROVED_CHANNEL
    timeout: float = 15.0
    simulated: ClassVar[bool] = False
    client: WebClient = field(init=False)

    def __post_init__(self) -> None:
        problem = channel_problem(self.channel)
        if problem:
            raise ValueError(problem)
        self.client = WebClient(
            token=self.token, timeout=int(self.timeout), retry_handlers=[]
        )

    def post(self, text: str, *, thread_ts: str = "") -> str:
        """Post to the approved channel and return the message's `ts`."""
        response = self._answer(
            lambda: self.client.chat_postMessage(
                channel=self.channel,
                text=text,
                thread_ts=thread_ts or None,
                unfurl_links=False,
                unfurl_media=False,
            )
        )
        return str(response.get("ts") or "")

    def upload(
        self, path: Path, *, title: str, comment: str, thread_ts: str = ""
    ) -> str:
        """Upload one existing local file and return its Slack file id.

        `files_upload_v2` is a sequence of calls (reserve, PUT, complete), so
        a failure anywhere in it leaves an outcome the caller cannot infer;
        that is raised rather than answered with a file id.
        """
        if not path.is_file():
            raise Refused(f"no such file: {path.name}")
        response = self._answer(
            lambda: self.client.files_upload_v2(
                channel=self.channel,
                file=str(path),
                filename=path.name,
                title=title,
                initial_comment=comment,
                thread_ts=thread_ts or None,
            )
        )
        uploaded = response.get("file") or {}
        file_id = str(uploaded.get("id") or "")
        if not file_id:
            # The call answered without naming a file: whether anything was
            # stored is exactly the question an upload id would answer.
            raise Ambiguous("upload completed without a file id")
        return file_id

    def _answer(self, call: Callable[[], SlackResponse]) -> dict[str, Any]:
        try:
            response = call()
        except SlackApiError as exc:
            # Slack answered: the call arrived and was rejected.
            raise SlackRejected(_reason(exc)) from None
        except Exception as exc:  # noqa: BLE001 - transport failures, not answers
            # No answer. A write may still have been applied, and posting the
            # same line twice is worse than a row that says "unknown".
            raise Ambiguous(type(exc).__name__) from None
        data = response.data if isinstance(response.data, dict) else {}
        if not data.get("ok", False):
            raise SlackRejected(str(data.get("error") or "ok: false"))
        return data


def _reason(exc: SlackApiError) -> str:
    """Slack's own error code, never the request that carried the token."""
    response = exc.response
    body = response.data if isinstance(response, SlackResponse) else None
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return type(exc).__name__
