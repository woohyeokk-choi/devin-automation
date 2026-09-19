"""Minimal Superset HTTP client used by the seed and scenario scripts.

Uses the ordinary browser login flow (session cookie + CSRF token) rather than
the JWT API login, because the explore form-data scenarios depend on the Flask
session id that only the cookie flow establishes.
"""

from __future__ import annotations

import os
import re
from typing import Any

import requests

BASE_URL = os.environ.get("SUPERSET_BASE_URL", "http://localhost:8088")
USERNAME = os.environ.get("SUPERSET_USERNAME", "admin")
PASSWORD = os.environ.get("SUPERSET_PASSWORD", "admin")

_CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


class SupersetClient:
    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.csrf_token: str | None = None

    # ---------------------------------------------------------------- auth
    def login(self, username: str = USERNAME, password: str = PASSWORD) -> None:
        page = self.session.get(f"{self.base_url}/login/", timeout=30)
        page.raise_for_status()
        match = _CSRF_RE.search(page.text)
        if not match:
            raise RuntimeError("csrf_token not found on /login/")
        resp = self.session.post(
            f"{self.base_url}/login/",
            data={
                "username": username,
                "password": password,
                "csrf_token": match.group(1),
            },
            allow_redirects=False,
            timeout=30,
        )
        if resp.status_code not in (302, 303):
            raise RuntimeError(f"login failed: {resp.status_code}")
        token = self.session.get(
            f"{self.base_url}/api/v1/security/csrf_token/", timeout=30
        )
        token.raise_for_status()
        self.csrf_token = token.json()["result"]

    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-CSRFToken": self.csrf_token or "",
            "Referer": self.base_url + "/",
        }

    # ------------------------------------------------------------- generic
    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        headers = self.headers()
        headers.update(kwargs.pop("headers", {}))
        return self.session.request(
            method, f"{self.base_url}{path}", headers=headers, timeout=60, **kwargs
        )

    def get(self, path: str, **kw: Any) -> requests.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> requests.Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> requests.Response:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> requests.Response:
        return self.request("DELETE", path, **kw)

    # ----------------------------------------------------------- resources
    def find_database(self, name: str) -> int | None:
        resp = self.get("/api/v1/database/")
        resp.raise_for_status()
        for item in resp.json()["result"]:
            if item["database_name"] == name:
                return int(item["id"])
        return None

    def create_database(self, name: str, uri: str) -> int:
        resp = self.post(
            "/api/v1/database/",
            json={
                "database_name": name,
                "sqlalchemy_uri": uri,
                "expose_in_sqllab": True,
            },
        )
        resp.raise_for_status()
        return int(resp.json()["id"])

    def find_dataset(self, table_name: str) -> int | None:
        resp = self.get("/api/v1/dataset/")
        resp.raise_for_status()
        for item in resp.json()["result"]:
            if item["table_name"] == table_name:
                return int(item["id"])
        return None

    def create_dataset(self, database_id: int, schema: str, table: str) -> int:
        resp = self.post(
            "/api/v1/dataset/",
            json={"database": database_id, "schema": schema, "table_name": table},
        )
        resp.raise_for_status()
        return int(resp.json()["id"])
