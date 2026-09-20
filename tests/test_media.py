"""Where a capture may be fetched from, and what may be fetched.

A session attachment arrives as a signed URL, which is a bearer token
pointed at somewhere this host then connects to. These are the rules that
keep that from becoming a fetch of anything, from anywhere.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest

from portal import media

ALLOWED = ("attachments.devin.ai",)
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"payload" * 64


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "video/mp4") -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}
        self._read = False

    def read(self, size: int) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def serve(
    monkeypatch: pytest.MonkeyPatch, pages: dict[str, Any], seen: list[str]
) -> None:
    """Answer each URL with a response or raise its scripted error."""

    class Opener:
        def open(self, request: Any, timeout: float = 0) -> Any:
            seen.append(request.full_url)
            answer = pages[request.full_url]
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(media.urllib.request, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(media.socket, "getaddrinfo", _public)


def _public(host: str, port: int, **kwargs: Any) -> list[Any]:
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def _private(host: str, port: int, **kwargs: Any) -> list[Any]:
    return [(2, 1, 6, "", ("169.254.169.254", port))]


def redirect(to: str) -> urllib.error.HTTPError:
    import email.message

    headers = email.message.Message()
    headers["Location"] = to
    return urllib.error.HTTPError("https://x", 302, "found", headers, None)


# --- where a capture may come from -----------------------------------------


def test_a_capture_is_fetched_only_from_an_allowed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media.socket, "getaddrinfo", _public)

    assert media.destination_problem("https://attachments.devin.ai/a.mp4", ALLOWED) == ""
    elsewhere = media.destination_problem("https://evil.example.com/a.mp4", ALLOWED)
    assert "not an allowed attachment host" in elsewhere
    # The refusal names the host, never the signed URL.
    assert "a.mp4" not in elsewhere


def test_a_suffix_rule_does_not_match_a_lookalike_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media.socket, "getaddrinfo", _public)
    hosts = (".devin.ai",)

    assert media.destination_problem("https://files.devin.ai/a.mp4", hosts) == ""
    assert media.destination_problem("https://devin.ai/a.mp4", hosts) == ""
    assert "not an allowed" in media.destination_problem(
        "https://devin.ai.evil.com/a.mp4", hosts
    )


def test_plain_http_credentials_and_private_addresses_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media.socket, "getaddrinfo", _public)

    assert "https" in media.destination_problem("http://attachments.devin.ai/a", ALLOWED)
    assert "credentials" in media.destination_problem(
        "https://user:secret@attachments.devin.ai/a", ALLOWED
    )

    monkeypatch.setattr(media.socket, "getaddrinfo", _private)
    assert "private address" in media.destination_problem(
        "https://attachments.devin.ai/a", ALLOWED
    )


def test_the_allowlist_comes_from_the_deployment_or_its_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PORTAL_MEDIA_HOSTS", raising=False)
    assert media.allowed_hosts() == media.DEFAULT_HOSTS

    monkeypatch.setenv("PORTAL_MEDIA_HOSTS", "attachments.devin.ai, storage.example.com")
    assert media.allowed_hosts() == ("attachments.devin.ai", "storage.example.com")


# --- and what comes back ---------------------------------------------------


def test_a_capture_downloads_from_an_allowed_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://attachments.devin.ai/a.mp4"
    seen: list[str] = []
    serve(monkeypatch, {url: FakeResponse(MP4)}, seen)

    written = media.download(url, tmp_path / "a.mp4", hosts=ALLOWED)

    assert written.read_bytes() == MP4 and seen == [url]


def test_a_redirect_is_judged_by_the_same_rules_as_the_first_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = "https://attachments.devin.ai/a.mp4"
    inside = "https://attachments.devin.ai/signed/a.mp4"
    outside = "https://evil.example.com/a.mp4"
    seen: list[str] = []
    serve(
        monkeypatch,
        {first: redirect(inside), inside: FakeResponse(MP4), outside: FakeResponse(MP4)},
        seen,
    )

    assert media.download(first, tmp_path / "a.mp4", hosts=ALLOWED).is_file()
    assert seen == [first, inside]

    seen.clear()
    serve(monkeypatch, {first: redirect(outside), outside: FakeResponse(MP4)}, seen)
    with pytest.raises(media.MediaError, match="not an allowed attachment host"):
        media.download(first, tmp_path / "b.mp4", hosts=ALLOWED)
    assert seen == [first]  # the destination was never contacted
    assert not (tmp_path / "b.mp4").exists()


def test_our_credential_reaches_the_api_and_no_host_it_redirects_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API needs our key; the storage it hands off to must never see it."""
    api = "https://api.devin.ai/v3/organizations/org-1/attachments/u/a.mp4"
    storage = "https://attachments.devin.ai/signed/a.mp4"
    carried: list[str | None] = []

    class Opener:
        def open(self, request: Any, timeout: float = 0) -> Any:
            carried.append(request.get_header("Authorization"))
            if request.full_url == api:
                raise redirect(storage)
            return FakeResponse(MP4)

    monkeypatch.setattr(media.urllib.request, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(media.socket, "getaddrinfo", _public)

    media.download(
        api,
        tmp_path / "a.mp4",
        hosts=("api.devin.ai", "attachments.devin.ai"),
        bearer="cog_secret",
        bearer_origin="https://api.devin.ai",
    )
    assert carried == ["Bearer cog_secret", None]

    # A first URL that is not the trusted origin carries nothing either.
    carried.clear()
    media.download(
        storage,
        tmp_path / "b.mp4",
        hosts=("attachments.devin.ai",),
        bearer="cog_secret",
        bearer_origin="https://api.devin.ai",
    )
    assert carried == [None]


def test_a_redirect_that_downgrades_the_scheme_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = "https://attachments.devin.ai/a.mp4"
    serve(monkeypatch, {first: redirect("http://attachments.devin.ai/a.mp4")}, [])

    with pytest.raises(media.MediaError, match="https"):
        media.download(first, tmp_path / "a.mp4", hosts=ALLOWED)


def test_a_redirect_loop_ends_rather_than_running_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://attachments.devin.ai/a.mp4"
    seen: list[str] = []
    serve(monkeypatch, {url: redirect(url)}, seen)

    with pytest.raises(media.MediaError, match="too many times"):
        media.download(url, tmp_path / "a.mp4", hosts=ALLOWED)
    assert len(seen) == media.MAX_REDIRECTS + 1


def test_something_that_is_not_a_video_is_not_kept_for_slack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://attachments.devin.ai/a.mp4"
    serve(monkeypatch, {url: FakeResponse(b"<html>nope</html>", "text/html")}, [])
    with pytest.raises(media.MediaError, match="not a video"):
        media.download(url, tmp_path / "a.mp4", hosts=ALLOWED)
    assert not (tmp_path / "a.mp4").exists()

    serve(monkeypatch, {url: FakeResponse(b"not an mp4 at all", "video/mp4")}, [])
    with pytest.raises(media.MediaError, match="not an MP4"):
        media.download(url, tmp_path / "a.mp4", hosts=ALLOWED)
    assert not (tmp_path / "a.mp4").exists()

    # Too short to carry a box header is unidentifiable, not acceptable.
    serve(monkeypatch, {url: FakeResponse(b"\x00", "video/mp4")}, [])
    with pytest.raises(media.MediaError, match="not an MP4"):
        media.download(url, tmp_path / "a.mp4", hosts=ALLOWED)
    assert not (tmp_path / "a.mp4").exists()


def test_a_download_is_bounded_in_size_and_in_total_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://attachments.devin.ai/a.mp4"
    serve(monkeypatch, {url: FakeResponse(MP4)}, [])
    with pytest.raises(media.MediaError, match="larger than"):
        media.download(url, tmp_path / "a.mp4", hosts=ALLOWED, max_bytes=8)
    assert not (tmp_path / "a.mp4").exists()

    # A stream that keeps trickling satisfies an inactivity timeout forever;
    # the elapsed bound is what ends it.
    serve(monkeypatch, {url: FakeResponse(MP4)}, [])
    ticks = iter([0.0, 1.0, 10_000.0, 10_001.0])
    monkeypatch.setattr(media.time, "monotonic", lambda: next(ticks))
    with pytest.raises(media.MediaError, match="longer than"):
        media.download(url, tmp_path / "a.mp4", hosts=ALLOWED)
    assert not (tmp_path / "a.mp4").exists()
