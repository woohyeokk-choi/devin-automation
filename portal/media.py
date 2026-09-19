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
* the download is bounded in size and time and carries no credential of ours.
  The listing's URL is signed, which makes it a bearer token in its own
  right: it is never logged, stored or published, and no Authorization
  header is forwarded to whatever host it points at.
"""

from __future__ import annotations

import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: `post-merge-<40 hex>-<YYYYMMDDTHHMMSSZ>-<case>.mp4`, as the brief asks for
#: it. The name is the recorder's claim about its own capture; the host
#: checks it against the commit it accepted before anything is published.
CAPTURE_NAME = re.compile(
    r"^post-merge-(?P<sha>[0-9a-f]{40})-"
    r"(?P<stamp>\d{8}T\d{6}Z)-(?P<case>[A-Za-z0-9_]{1,12})\.mp4$"
)

#: A screen capture of one user action. Anything larger is refused rather
#: than streamed onto the host.
MAX_BYTES = 256 * 1024 * 1024
TIMEOUT_SECONDS = 180


class MediaError(RuntimeError):
    """The capture could not be read. Never a verdict about the code."""


@dataclass(frozen=True)
class Capture:
    """What a capture says about itself."""

    name: str
    sha: str
    case: str
    recorded_at: str


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
    )


def capture_problem(capture: Capture, merge_sha: str, case: str) -> str:
    """Why this capture cannot stand for the merged commit, if it cannot."""
    if capture.sha != merge_sha.strip().lower():
        return (
            f"the capture names {capture.sha[:12]}, not the merged commit "
            f"{merge_sha[:12]}"
        )
    if case and capture.case != case.upper():
        return f"the capture is of {capture.case}, not {case.upper()}"
    return ""


def download(
    url: str,
    destination: Path,
    *,
    max_bytes: int = MAX_BYTES,
    timeout: int = TIMEOUT_SECONDS,
) -> Path:
    """Fetch one capture to `destination`, bounded, with no credential of ours.

    The URL is pre-signed by whoever issued the listing, so sending our own
    Authorization header with it would hand that credential to a storage
    host; the request carries none. A response that runs past `max_bytes` is
    abandoned and the partial file removed, so a wrong or hostile URL cannot
    fill the disk.
    """
    if not url.startswith("https://"):
        raise MediaError("a capture download must be https")
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, method="GET")
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with destination.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise MediaError(
                            f"the capture is larger than {max_bytes} bytes"
                        )
                    handle.write(chunk)
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
    return destination


def clear(directory: Path) -> None:
    """Remove a run's downloaded captures once they have been published."""
    shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "CAPTURE_NAME",
    "Capture",
    "MediaError",
    "capture_of",
    "capture_problem",
    "clear",
    "download",
]
