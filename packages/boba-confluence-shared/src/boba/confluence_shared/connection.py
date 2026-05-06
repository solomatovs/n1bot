"""Общий connection-config для Confluence (base_url + auth + timeout).

Используется индексатор-плагинами (`confluence_space/pages/cql`) и tool'ом
агента (`confluence_search`), плюс CLI оператора. Раньше каждый плагин
дублировал 5 одинаковых FieldSpec и `match cfg.auth_method` — теперь это
делает `ConfluenceConnection`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

import httpx

from boba.coercion import (
    ChainCoercer,
    Default,
    OneOf,
    ParseFloat,
    ParseString,
    RequiredWhen,
)
from boba.declaration import FieldSpec
from boba.http_transport import BasicAuth, HttpTransport, PatAuth

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


class ConfluenceConnection:
    """Helpers поверх `ConfluenceConnectionConfig`: схема + auth/transport/client."""

    @staticmethod
    def fields() -> list[FieldSpec]:
        """5 готовых FieldSpec для встраивания в `ObjectSchema(fields=[...])`."""
        return [
            FieldSpec(
                name="base_url",
                coercer=ParseString(),
                required=True,
                description="URL Confluence (env: ...__BASE_URL).",
            ),
            FieldSpec(
                name="auth_method",
                coercer=ChainCoercer(
                    Default("pat"), ParseString(), OneOf("pat", "basic"),
                ),
                description="`pat` — Bearer-токен; `basic` — login+password.",
            ),
            FieldSpec(
                name="auth_user",
                coercer=ChainCoercer(Default(""), ParseString()),
                description="Логин для basic-auth; для PAT — пусто.",
            ),
            FieldSpec(
                name="auth_token",
                coercer=ParseString(),
                required=True,
                description="PAT или пароль (env: ...__AUTH_TOKEN).",
            ),
            FieldSpec(
                name="timeout_sec",
                coercer=ChainCoercer(Default(30.0), ParseFloat()),
                description="HTTP-таймаут (сек).",
            ),
        ]

    @staticmethod
    def invariant() -> RequiredWhen:
        """Object-level invariant: при `auth_method=basic` обязателен `auth_user`."""
        return RequiredWhen("auth_method", "basic", "auth_user")

    @staticmethod
    def make_auth(cfg: ConfluenceConnectionConfig) -> BasicAuth | PatAuth:
        match cfg.auth_method:
            case "basic":
                return BasicAuth(user=cfg.auth_user, password=cfg.auth_token)
            case "pat":
                return PatAuth(token=cfg.auth_token)
            case _:
                msg = f"Unsupported auth_method: {cfg.auth_method!r}"
                raise ValueError(msg)

    @staticmethod
    def make_transport(cfg: ConfluenceConnectionConfig) -> HttpTransport:
        return HttpTransport(timeout_sec=cfg.timeout_sec)

    @classmethod
    def make_discovery_client(
        cls, cfg: ConfluenceConnectionConfig,
    ) -> httpx.Client:
        """httpx.Client для side-channel REST-запросов (discovery, search).

        Отдельный от Transport: Transport отвечает за выгрузку content,
        этот client — за discovery (CQL/page-listings/search). Без цикла.
        """
        kwargs: dict[str, Any] = {
            "base_url": cfg.base_url.rstrip("/"),
            "timeout": cfg.timeout_sec,
        }
        cls.make_auth(cfg)(kwargs)
        return httpx.Client(**kwargs)

    @classmethod
    def all_field_names(cls) -> Iterable[str]:
        """Имена 5 connection-полей — для DTO factory introspection."""
        return (f.name for f in cls.fields())
