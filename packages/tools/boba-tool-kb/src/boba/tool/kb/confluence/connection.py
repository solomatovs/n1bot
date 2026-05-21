"""Helpers поверх `ConfluenceConnectionConfig`: auth + transport фабрики.

Доменный слой: фабрики auth/transport, БЕЗ httpx-клиента. httpx-клиенты
живут глубже в pipeline (`HttpTransport`, RequestSources). Прокси-Protocol
вокруг конфига больше не нужен — есть единственный концертный класс
`ConfluenceConnectionConfig` (см. `config.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from boba.tool.kb.confluence.auth import PatAuth
from boba.transport.http import HttpTransport

if TYPE_CHECKING:
    from boba.tool.kb.confluence.config import ConfluenceConnectionConfig

__all__ = ["ConfluenceConnection"]


class ConfluenceConnection:
    """Helpers поверх `ConfluenceConnectionConfig`: auth/transport.

    Object-level invariant (`при auth_method=basic обязателен auth_user`)
    проверяется в `ConfluenceConnectionConfig._validate`.
    """

    @staticmethod
    def make_auth(cfg: ConfluenceConnectionConfig) -> httpx.Auth | None:
        """`auth_method=none` → `None` (anonymous для публичного Confluence).

        Downstream (`HttpRequest.auth`, `httpx.Client(auth=...)`, RequestSources)
        принимают `httpx.Auth | None`, поэтому None прокидывается до конца pipeline.
        """
        match cfg.auth_method:
            case "none":
                return None
            case "basic":
                return httpx.BasicAuth(
                    username=cfg.auth_user,
                    password=cfg.auth_token,
                )
            case "pat":
                return PatAuth(token=cfg.auth_token)
            case _:
                msg = f"Unsupported auth_method: {cfg.auth_method!r}"
                raise ValueError(msg)

    @staticmethod
    def make_transport(cfg: ConfluenceConnectionConfig) -> HttpTransport:
        return HttpTransport(
            timeout_sec=cfg.timeout_sec,
            verify=cfg.ssl_verify,
        )
