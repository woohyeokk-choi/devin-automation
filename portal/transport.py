"""The one place an outbound HTTP call is made.

Everything the controller sends leaves through a `Transport`, so the offline
tests exercise the real client code with a fake wire instead of a mocked-out
client. The distinction the controller depends on is the three outcomes:

* a response (any status, including 401/403/429 — those are answers);
* a refusal (the request provably never reached the server);
* an *ambiguous* failure (a timeout or a dropped connection after the request
  was written), where a write may or may not have happened. That one is not an
  error to retry blindly; it is the case reconciliation exists for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import requests
from urllib3.exceptions import NewConnectionError

try:  # urllib3 >= 2 splits DNS failures out of NewConnectionError
    from urllib3.exceptions import NameResolutionError

    _DNS_FAILURE: type[Exception] = NameResolutionError
except ImportError:  # pragma: no cover - urllib3 1.x
    _DNS_FAILURE = NewConnectionError

#: The only failures that prove the request never reached the server. A
#: `requests.ConnectionError` is also raised for a reset or a dropped socket
#: *after* the body was written (the HTTP adapter wraps `ProtocolError` and a
#: bare `OSError` the same way), so the class alone cannot be read as
#: "nothing was sent".
_PRE_SEND = (NewConnectionError, _DNS_FAILURE)


def _never_sent(exc: BaseException) -> bool:
    if isinstance(exc, requests.ConnectTimeout):
        return True
    cause: BaseException | None = exc
    for _ in range(10):
        if cause is None:
            break
        if isinstance(cause, _PRE_SEND):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


class Ambiguous(Exception):
    """The request may have been applied. Reconcile; do not retry blindly."""


class Refused(Exception):
    """The request provably never reached the server."""


@dataclass(frozen=True)
class Response:
    status: int
    body: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error(self) -> str:
        """A short reason, from the documented problem body where present."""
        detail = self.body.get("detail") or self.body.get("message") or ""
        return f"HTTP {self.status}" + (f": {detail}" if detail else "")


class Transport(Protocol):
    #: False for a wire that reaches the network, True for a scripted one.
    #: A simulated repair must not be able to reach a real destination just
    #: because the process that runs it happens to hold a real credential,
    #: so callers that own a credential read this before using it.
    simulated: bool

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Response: ...


def is_simulated(transport: Transport) -> bool:
    """Whether this wire is scripted. A wire that does not say counts as one.

    Fail closed: the question is only ever asked before using a real
    credential, and "unsure" is not permission.
    """
    try:
        return bool(transport.simulated)
    except AttributeError:
        return True


@dataclass
class HttpTransport:
    """`requests` with a hard timeout and no retries of its own.

    `allow_redirects` is a parameter because one caller must refuse them: a
    redirect on a webhook post would deliver the body to whatever host the
    response names.
    """

    timeout: float = 30.0
    allow_redirects: bool = True
    simulated: ClassVar[bool] = False

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Response:
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=json,
                params=params,
                timeout=self.timeout,
                allow_redirects=self.allow_redirects,
            )
        except requests.ConnectionError as exc:
            if _never_sent(exc):
                # The connection was never established.
                raise Refused(type(exc).__name__) from None
            # A reset or a drop that may have followed a complete write.
            raise Ambiguous(f"{type(exc).__name__} (connection lost)") from None
        except requests.RequestException as exc:
            # Timeouts and mid-flight failures: the server may have acted.
            raise Ambiguous(type(exc).__name__) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        return Response(response.status_code, body if isinstance(body, dict) else {"items": body})
