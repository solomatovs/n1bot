"""Общий connection-конфиг для Confluence (base_url + auth + timeout).

Доменный слой: конфиг-схема + auth/transport фабрики, БЕЗ httpx-клиента.
httpx-фабрики живут в ext-пакетах (`http_client.py` в runtime/indexing).
"""

from __future__ import annotations

from typing import Protocol

import httpx

from boba.tool.kb.confluence.auth import PatAuth
from boba.transport.http import HttpTransport

__all__ = [
    "ConfluenceConnection",
    "ConfluenceConnectionConfig",
]


class ConfluenceConnectionConfig(Protocol):
    """Минимальный контракт DTO с connection-полями.

    Любой `@dataclass(frozen=True)`, у которого есть эти поля, удовлетворяет
    протоколу — каждый pipeline-плагин сам объявляет свой Config с
    дополнительными специфичными полями (cql, space_key, page_ids, ...) и
    автоматически подходит как аргумент `ConfluenceConnection.make_*`.

    Поля объявлены как `@property` (read-only), чтобы протокол был совместим
    с frozen-dataclass-атрибутами (которые тоже read-only).
    """

    @property
    def base_url(self) -> str: ...
    @property
    def auth_method(self) -> str: ...
    @property
    def auth_user(self) -> str: ...
    @property
    def auth_token(self) -> str: ...
    @property
    def timeout_sec(self) -> float: ...
    @property
    def ssl_verify(self) -> bool: ...


class ConfluenceConnection:
    """Helpers поверх `ConfluenceConnectionConfig`: auth/transport.

    Object-level invariant `при auth_method=basic обязателен auth_user`
    реализован прямо в `ConfluencePluginConfig._check_invariants`
    (`@model_validator(mode='after')`); см. `plugin.py`.
    """

    @staticmethod
    def make_auth(cfg: ConfluenceConnectionConfig) -> httpx.Auth | None:
        """`auth_method=none` → `None` (anonymous-доступ к публичному Confluence).

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
            verify=cfg.ssl_verify
        )
