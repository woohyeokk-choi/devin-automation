"""The session's own capture of merged code, read back by the host.

After a human merge the host has already decided the outcome; what the
session still owes the demo is footage of the merged commit running. It
delivers that as a session attachment, and this module is how the host picks
it up:

* the capture has to name what it is — case, full commit id and capture time
  live in the file name the brief dictates, so the metadata comes from the
  recorder rather than being inferred from the repair it is attached to;
* only files the agent produced count (``source`` is ``devin``): a file an
  operator uploaded earlier proves nothing about a commit that did not exist
  then;
* the download goes only to the attachment service the listing belongs to.
  A signed URL is a bearer token in its own right: it is never logged,
  stored or published, no Authorization header of ours is forwarded with
  it, and neither the URL nor a redirect it hands back may send the request
  anywhere but an allowed public host. Every redirect is re-checked, and
  the whole transfer is bounded in size and in total elapsed time, not only
  in inactivity.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: The two things a session is ever asked to record: the failure as it
#: stands on the baseline, and the same action on the merged commit. They
#: are different evidence about different code and are never interchangeable.
SYMPTOM = "symptom"
AFTER_MERGE = "post-merge"
STAGES = (SYMPTOM, AFTER_MERGE)

#: `<stage>-<40 hex>-<YYYYMMDDTHHMMSSZ>-<case>.mp4`, as the brief asks for
#: it. The name is the recorder's claim about its own capture; the host
#: checks it against the commit it accepted before anything is published.
CAPTURE_NAME = re.compile(
    r"^(?P<stage>symptom|post-merge)-(?P<sha>[0-9a-f]{40})-"
    r"(?P<stamp>\d{8}T\d{6}Z)-(?P<case>[A-Za-z0-9_]{1,12})\.mp4$"
)

#: A screen capture of one user action. Anything larger is refused rather
#: than streamed onto the host.
MAX_BYTES = 256 * 1024 * 1024
#: Inactivity timeout per socket operation, and a bound on the whole
#: transfer: a stream that keeps trickling bytes would satisfy the first
#: forever, so the second is what actually ends it.
TIMEOUT_SECONDS = 180
TOTAL_SECONDS = 600
MAX_REDIRECTS = 3

#: Where a Devin session attachment may be fetched from. A leading dot is a
#: suffix match on the registered name, anything else is exact. The
#: attachment service is the only destination the listing is expected to
#: name; `PORTAL_MEDIA_HOSTS` lets an operator add the storage host this
#: deployment actually observes rather than having a wildcard stand in for
#: it. A host that is not listed is refused by name, never by URL.
DEFAULT_HOSTS = (".devin.ai", ".cognition.ai")

#: A capture is a video file. The response has to say so, and the bytes
#: have to agree: an ISO base-media file names its brand in the second box.
VIDEO_TYPES = ("video/mp4", "video/quicktime", "application/octet-stream")


class MediaError(RuntimeError):
    """The capture could not be read. Never a verdict about the code."""


@dataclass(frozen=True)
class Capture:
    """What a capture says about itself."""

    name: str
    sha: str
    case: str
    recorded_at: str
    stage: str = AFTER_MERGE


def capture_of(name: str) -> Capture | None:
    """Read a capture's own metadata out of its name, or refuse it."""
    match = CAPTURE_NAME.match(name.strip())
    if match is None:
        return None
    stamp = match.group("stamp")
    try:
        recorded = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return Capture(
        name=name.strip(),
        sha=match.group("sha").lower(),
        case=match.group("case").upper(),
        recorded_at=recorded.isoformat(),
        stage=match.group("stage"),
    )


def capture_problem(
    capture: Capture, sha: str, case: str, stage: str = AFTER_MERGE
) -> str:
    """Why this capture cannot stand for the commit asked about, if it cannot.

    The stage is part of the claim: footage of the baseline failing and
    footage of the merged commit working are both real captures, and either
    one presented as the other would be a false statement about the code.
    """
    if capture.stage != stage:
        return f"the capture is {capture.stage} footage, not {stage}"
    if capture.sha != sha.strip().lower():
        return f"the capture names {capture.sha[:12]}, not {sha[:12]}"
    if case and capture.case != case.upper():
        return f"the capture is of {capture.case}, not {case.upper()}"
    return ""


def allowed_hosts() -> tuple[str, ...]:
    """The attachment destinations this deployment accepts."""
    configured = os.environ.get("PORTAL_MEDIA_HOSTS", "").strip()
    if not configured:
        return DEFAULT_HOSTS
    return tuple(part.strip().lower() for part in configured.split(",") if part.strip())


def _host_allowed(host: str, hosts: tuple[str, ...]) -> bool:
    for allowed in hosts:
        if allowed.startswith("."):
            if host == allowed[1:] or host.endswith(allowed):
                return True
        elif host == allowed:
            return True
    return False


def destination_problem(url: str, hosts: tuple[str, ...]) -> str:
    """Why this is not somewhere a capture may be fetched from.

    Named so a redirect can be judged by exactly the rules the first URL
    was: a redirect that escapes the allowlist, drops to http, carries
    credentials in the authority, or resolves onto this host's own network
    is the same problem arriving later.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "the capture location could not be parsed"
    if parts.scheme != "https":
        return f"a capture download must be https, not {parts.scheme or 'nothing'}"
    if parts.username or parts.password:
        return "the capture location carries credentials in its authority"
    host = (parts.hostname or "").lower()
    if not host:
        return "the capture location names no host"
    if not _host_allowed(host, hosts):
        return f"{host} is not an allowed attachment host"
    try:
        resolved = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return f"{host} could not be resolved"
    for info in resolved:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_loopback or address.is_private:
            return f"{host} resolves onto a private address"
    return ""


def _video_problem(content_type: str, head: bytes) -> str:
    """Whether this is the video it claims to be, before Slack is offered it."""
    kind = content_type.split(";")[0].strip().lower()
    if kind and kind not in VIDEO_TYPES:
        return f"the capture is {kind}, not a video"
    if len(head) >= 12 and head[4:8] != b"ftyp":
        return "the capture is not an MP4 file"
    return ""


def download(
    url: str,
    destination: Path,
    *,
    max_bytes: int = MAX_BYTES,
    timeout: int = TIMEOUT_SECONDS,
    total_seconds: int = TOTAL_SECONDS,
    hosts: tuple[str, ...] | None = None,
) -> Path:
    """Fetch one capture to `destination`, bounded, with no credential of ours.

    The URL is pre-signed by whoever issued the listing, so sending our own
    Authorization header with it would hand that credential to a storage
    host; the request carries none, and it is only ever sent to an allowed
    attachment host. Redirects are followed by hand so each hop is judged by
    those same rules instead of being taken on trust, and a response that
    outruns `max_bytes` or `total_seconds` is abandoned with its partial
    file removed.
    """
    permitted = hosts if hosts is not None else allowed_hosts()
    deadline = time.monotonic() + total_seconds
    location = url
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        for hop in range(MAX_REDIRECTS + 1):
            problem = destination_problem(location, permitted)
            if problem:
                raise MediaError(problem)
            request = urllib.request.Request(location, method="GET")
            opener = urllib.request.build_opener(_NoRedirect)
            try:
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308):
                    raise MediaError(f"the capture service answered {exc.code}")
                if hop >= MAX_REDIRECTS:
                    raise MediaError("the capture redirected too many times")
                nxt = exc.headers.get("Location") or ""
                exc.close()
                if not nxt:
                    raise MediaError("the capture redirected to nowhere")
                location = urllib.parse.urljoin(location, nxt)
                continue
            with response:
                head = b""
                with destination.open("wb") as handle:
                    while True:
                        if time.monotonic() > deadline:
                            raise MediaError(
                                f"the capture took longer than {total_seconds}s"
                            )
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise MediaError(
                                f"the capture is larger than {max_bytes} bytes"
                            )
                        if len(head) < 12:
                            head += chunk[: 12 - len(head)]
                        handle.write(chunk)
                kind = str(response.headers.get("Content-Type") or "")
            break
        else:  # pragma: no cover - the loop always breaks or raises
            raise MediaError("the capture redirected too many times")
    except MediaError:
        destination.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        # The URL is a credential: only the failure kind is reported.
        raise MediaError(f"the capture could not be downloaded ({type(exc).__name__})")
    if written == 0:
        destination.unlink(missing_ok=True)
        raise MediaError("the capture downloaded as an empty file")
    wrong = _video_problem(kind, head)
    if wrong:
        destination.unlink(missing_ok=True)
        raise MediaError(wrong)
    return destination


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand redirects back to the caller, which checks where they point."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def clear(directory: Path) -> None:
    """Remove a run's downloaded captures once they have been published."""
    shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "AFTER_MERGE",
    "CAPTURE_NAME",
    "STAGES",
    "SYMPTOM",
    "DEFAULT_HOSTS",
    "allowed_hosts",
    "destination_problem",
    "Capture",
    "MediaError",
    "capture_of",
    "capture_problem",
    "clear",
    "download",
]
