"""Вход в стенд сервиса boba-mcp от имени пользователя.

Ошибки:
ServiceSignInError — сервис не начал слушать за срок ожидания либо отказал
    в обмене утверждения на токен.
"""

from __future__ import annotations

import time
from typing import ClassVar

import httpx

from boba.auth.proxy import ProxyAssertions

__all__ = ["ServiceSignIn", "ServiceSignInError"]


class ServiceSignInError(Exception):
    """Стенд сервиса не выдал токен доступа."""


class ServiceSignIn:
    """Токен доступа endpoint'а стенда сервиса boba-mcp для пользователя.

    Создаётся тестом или стендом, которому нужен вызов сервиса от имени
    названного пользователя. Вход — тот же, что у чата: утверждение о
    пользователе подписывается ключом proxy стенда и меняется на /token
    сервиса на токен (grant jwt-bearer). Токен годен только на endpoint'е,
    чей адрес назван ресурсом. Запрос блокирующий: из цикла событий, в
    котором слушает сам сервис, его зовут через asyncio.to_thread.
    """

    GRANT: ClassVar[str] = "urn:ietf:params:oauth:grant-type:jwt-bearer"
    WAIT_SEC: ClassVar[float] = 30.0
    POLL_SEC: ClassVar[float] = 0.2

    def __init__(
        self, public_url: str, client_id: str, client_secret: str, proxy_secret: str
    ) -> None:
        self._public_url = public_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._assertions = ProxyAssertions(proxy_secret, 0)

    def token(self, login: str, roles: str, resource: str) -> str:
        """Токен пользователя login с ролями roles для endpoint'а resource."""
        form = {
            "grant_type": self.GRANT,
            "assertion": self._assertions.issue(
                self._client_id, login, roles, self._public_url, resource
            ),
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "resource": resource,
        }
        reply = self._posted(form)
        if reply.status_code != httpx.codes.OK:
            msg = (
                f"POST {self._public_url}/token for {login!r} with roles {roles!r} "
                f"and resource {resource}: expected 200 with a token, got "
                f"{reply.status_code}: {reply.text[:300]}"
            )
            raise ServiceSignInError(msg)

        return str(reply.json()["access_token"])

    def _posted(self, form: dict[str, str]) -> httpx.Response:
        """Ответ /token; сервис стенда может ещё подниматься — ждём порт."""
        deadline = time.monotonic() + self.WAIT_SEC
        while True:
            try:
                return httpx.post(f"{self._public_url}/token", data=form)
            except httpx.ConnectError as exc:
                if time.monotonic() >= deadline:
                    msg = (
                        f"POST {self._public_url}/token: the service did not "
                        f"listen within {self.WAIT_SEC}s: {exc}"
                    )
                    raise ServiceSignInError(msg) from exc

            time.sleep(self.POLL_SEC)
