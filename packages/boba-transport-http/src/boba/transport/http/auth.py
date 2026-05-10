"""HTTP-auth callables: PatAuth, BasicAuth.

`AuthApplier` — простой `Callable[[dict[str, Any]], None]`, мутирующий
`httpx.Client(**kwargs)` перед его созданием. RequestSource создаёт
нужный callable из своих credentials и кладёт в `HttpRequest.auth`.
HttpTransport вызывает callable, не зная про PAT/Basic/OAuth/...

Кастомная auth-схема — любая функция с такой сигнатурой; не обязательно
наследоваться или регистрироваться.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["AuthApplier", "BasicAuth", "PatAuth"]


AuthApplier = Callable[[dict[str, Any]], None]
"""Type alias: callable, мутирующий kwargs перед `httpx.Client(**kwargs)`."""


class PatAuth:
    """Atlassian Personal Access Token: Authorization: Bearer <token>."""

    def __init__(self, token: str) -> None:
        self._token = token

    def __call__(self, kwargs: dict[str, Any]) -> None:
        headers = dict(kwargs.get("headers") or {})
        headers["Authorization"] = f"Bearer {self._token}"
        kwargs["headers"] = headers


class BasicAuth:
    """HTTP basic-auth: httpx.Client(auth=(user, password))."""

    def __init__(self, user: str, password: str) -> None:
        self._user = user
        self._password = password

    def __call__(self, kwargs: dict[str, Any]) -> None:
        kwargs["auth"] = (self._user, self._password)
