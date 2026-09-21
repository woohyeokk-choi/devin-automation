"""What the wire can and cannot prove about a failed write.

`requests` raises `ConnectionError` both for a socket that was never opened
and for one that died after the body went out. Only the first kind may be
called refused; everything else has to stay ambiguous, because a repair
session may already exist on the other side.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests
from urllib3.exceptions import ProtocolError

from portal.transport import Ambiguous, HttpTransport, Refused


@pytest.fixture()
def send(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Attempt one write whose wire failure the test chooses."""

    def attempt(error: BaseException) -> None:
        def raise_it(*args: Any, **kwargs: Any) -> Any:
            raise error

        monkeypatch.setattr(requests, "request", raise_it)
        HttpTransport().request(
            "POST", "https://example.invalid/v3/sessions", headers={"A": "b"}
        )

    return attempt


def connection_error(cause: BaseException | None) -> requests.ConnectionError:
    error = requests.ConnectionError("connection problem")
    if cause is not None:
        error.__cause__ = cause
    return error


def test_a_connection_that_was_never_opened_is_refused(send: Any) -> None:
    from urllib3.exceptions import NewConnectionError

    never = NewConnectionError(None, "failed to establish a new connection")  # type: ignore[arg-type]
    with pytest.raises(Refused):
        send(connection_error(never))


def test_a_connect_timeout_is_refused(send: Any) -> None:
    with pytest.raises(Refused):
        send(requests.ConnectTimeout("timed out connecting"))


@pytest.mark.parametrize(
    "cause",
    [
        ProtocolError("connection aborted", ConnectionResetError(104, "reset by peer")),
        ConnectionResetError(104, "reset by peer"),
        OSError(32, "broken pipe"),
        None,
    ],
)
def test_a_connection_lost_around_the_write_is_ambiguous(
    send: Any, cause: BaseException
) -> None:
    """The adapter wraps post-write resets in the same exception class."""
    with pytest.raises(Ambiguous):
        send(connection_error(cause))


def test_a_read_timeout_is_ambiguous(send: Any) -> None:
    # The request was written; the answer never came back.
    with pytest.raises(Ambiguous):
        send(requests.ReadTimeout("timed out reading"))
