"""Atlassian-PAT auth: `Authorization: Bearer <token>` как `httpx.Auth`.

У httpx нет встроенного Bearer-класса, поэтому держим маленький
`httpx.Auth`-наследник рядом с остальной Confluence-инфраструктурой.
Для Basic auth используется `httpx.BasicAuth` напрямую — отдельная обёртка
не нужна.
"""

from __future__ import annotations

from collections.abc import Generator

import httpx

__all__ = ["PatAuth"]


class PatAuth(httpx.Auth):
    """Atlassian Personal Access Token: Authorization: Bearer <token>."""

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(
        self,
        request: httpx.Request,
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request
