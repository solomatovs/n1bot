"""HTTP-auth callables как `httpx.Auth`-наследники.

httpx даёт из коробки `BasicAuth`/`DigestAuth`/`NetRCAuth`, но не имеет
встроенного Bearer/PAT. Эта схема нужна разным трансport-консьюмерам
(Atlassian PAT, GitHub PAT, любые API с `Authorization: Bearer <token>`),
поэтому реализация живёт здесь, рядом с остальной HTTP-инфраструктурой,
а не в feature-пакетах.
"""

from __future__ import annotations

from collections.abc import Generator

import httpx

__all__ = ["HttpxBearerAuth"]


class HttpxBearerAuth(httpx.Auth):
    """`Authorization: Bearer <token>` для httpx.

    Используется любыми API, требующими Bearer-токен (Atlassian PAT,
    GitHub PAT, generic OAuth2 access token). Pure header-mutation,
    без refresh-flow.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(
        self,
        request: httpx.Request,
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request
