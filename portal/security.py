"""The demo gate: authentication, profile integrity and CSRF.

This is deliberately the *smallest* gate that makes the event log trustworthy,
not an IAM build. Its job is narrow: an event that a repair job may later be
built from must never originate from an anonymous or cross-origin client, and
the profile an event is attributed to must have been issued by this server.

Three parts:

1. **Demo login** (HTTP Basic). Every route except `/healthz` requires it;
   `/ops` additionally requires the operator credential.
2. **Signed profile cookie.** The browser picks a demo profile by *name* from a
   fixed server-side map; the cookie is HMAC-signed so a client cannot forge
   one, and an unknown or tampered profile is rejected rather than quietly
   falling back to the more privileged one.
3. **CSRF.** Every state-changing request must carry a token matching its
   signed cookie, and any cross-origin `Origin`/`Referer` is refused, so a
   third-party page cannot drive an authenticated demo session.
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from itsdangerous import BadSignature, URLSafeSerializer

from .config import settings

# Demo profiles are a fixed server-side map to Superset credentials. The
# browser only ever names one of these keys.
PROFILES = {
    "analyst": "Dana (analyst)",
    "restricted_viewer": "Robin (restricted viewer)",
}
DEFAULT_PROFILE = "analyst"
PROFILE_COOKIE = "portal_profile"
CSRF_COOKIE = "portal_csrf"
CSRF_FIELD = "csrf_token"

_basic = HTTPBasic(auto_error=False)
_signer = URLSafeSerializer(settings.cookie_secret, salt="portal-profile")


class Unauthenticated(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="demo login required",
            headers={"WWW-Authenticate": "Basic"},
        )


def _matches(credentials: HTTPBasicCredentials | None, user: str, password: str) -> bool:
    if credentials is None:
        return False
    return secrets.compare_digest(
        credentials.username, user
    ) and secrets.compare_digest(credentials.password, password)


def demo_guard(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    """Any invited demo user (the operator counts as one)."""
    if _matches(credentials, settings.demo_username, settings.demo_password):
        return settings.demo_username
    if _matches(credentials, settings.ops_username, settings.ops_password):
        return settings.ops_username
    raise Unauthenticated()


def ops_guard(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    if _matches(credentials, settings.ops_username, settings.ops_password):
        return settings.ops_username
    raise Unauthenticated()


# ------------------------------------------------------------------ profile
def sign_profile(profile: str) -> str:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}")
    return str(_signer.dumps(profile))


def profile_of(request: Request) -> str:
    """The profile this request is entitled to.

    Only an absent cookie yields the default. A cookie that fails its
    signature check, or names a profile that does not exist, is refused:
    quietly substituting the default would turn a forged `restricted_viewer`
    cookie into the fully capable `analyst` identity.
    """
    raw = request.cookies.get(PROFILE_COOKIE)
    if not raw:
        return DEFAULT_PROFILE
    try:
        profile = str(_signer.loads(raw))
    except BadSignature:
        raise HTTPException(status_code=400, detail="invalid demo profile cookie") from None
    return require_known_profile(profile)


def require_known_profile(profile: str) -> str:
    """Reject an unknown profile instead of falling back to a privileged one."""
    if profile not in PROFILES:
        raise HTTPException(status_code=400, detail="unknown demo profile")
    return profile


# --------------------------------------------------------------------- CSRF
def csrf_token_of(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)


def _same_origin(request: Request, header: str) -> bool:
    value = request.headers.get(header)
    if not value:
        return True  # absent is not evidence of a cross-origin request
    parts = urlsplit(value)
    return f"{parts.scheme}://{parts.netloc}" == str(request.base_url).rstrip("/")


async def csrf_guard(request: Request) -> None:
    """Refuse a state-changing request that a third-party page could have sent."""
    if not (_same_origin(request, "origin") and _same_origin(request, "referer")):
        raise HTTPException(status_code=403, detail="cross-origin request refused")
    cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    submitted = str(form.get(CSRF_FIELD) or "")
    if not cookie or not submitted or not secrets.compare_digest(cookie, submitted):
        raise HTTPException(status_code=403, detail="missing or stale form token")


def apply_session_cookies(response: Any, profile: str, csrf_token: str) -> None:
    """Attach the signed profile and the CSRF token to a response."""
    response.set_cookie(
        PROFILE_COOKIE, sign_profile(profile), httponly=True, samesite="strict"
    )
    response.set_cookie(CSRF_COOKIE, csrf_token, samesite="strict")
