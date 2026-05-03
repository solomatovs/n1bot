"""Общая ConfigSection [indexer.sources.confluence]: backend + auth.

Per-mode секции (`[indexer.sources.confluence.space]` etc) добавляют свои
поля поверх. Это даёт single-source-of-truth для base_url / auth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    OneOf,
    ParseFloat,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.confluence_source.auth import (
    AuthError,
    BasicAuth,
    ConfluenceAuth,
    PatAuth,
)

__all__ = [
    "ConfluenceCommonConfig",
    "ConfluenceCommonSection",
    "build_auth",
]


@dataclass(frozen=True)
class ConfluenceCommonConfig:
    """Общие поля для всех confluence-source режимов."""

    base_url: str = ""
    auth_method: str = "pat"  # "pat" | "basic"
    auth_user: str = ""
    auth_token: str = ""
    body_format: str = "export_view"  # "export_view" | "view" | "storage"
    timeout_sec: float = 30.0


class ConfluenceCommonSection(ConfigSection[ConfluenceCommonConfig]):
    """[indexer.sources.confluence] — backend и auth для всех режимов."""

    namespace: ClassVar[tuple[str, ...]] = ("indexer", "sources", "confluence")

    schema: ClassVar[ObjectSchema[ConfluenceCommonConfig]] = ObjectSchema(
        description=(
            "Confluence REST backend для индексации. base_url + auth + "
            "формат тела. Per-mode параметры (space_key, page_ids, cql) — в "
            "соответствующих [indexer.sources.confluence.<mode>] секциях."
        ),
        fields=[
            FieldSpec(
                name="base_url",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "База Confluence-инстанса (например 'https://confl.example.com'). "
                    "Чувствительная информация — задавай через env, не TOML: "
                    "BOBA_INDEXER__SOURCES__CONFLUENCE__BASE_URL=..."
                ),
            ),
            FieldSpec(
                name="auth_method",
                coercer=ChainCoercer(
                    Default("pat"), ParseString(), OneOf("pat", "basic")
                ),
                description=(
                    "'pat' (Bearer token, modern Atlassian) или 'basic' "
                    "(user+password, legacy / Server)."
                ),
            ),
            FieldSpec(
                name="auth_user",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "Имя пользователя для basic. Игнорируется при pat."
                ),
            ),
            FieldSpec(
                name="auth_token",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "PAT (для pat) или пароль/legacy-token (для basic). "
                    "ОБЯЗАТЕЛЬНО задавать через env (не TOML): "
                    "BOBA_INDEXER__SOURCES__CONFLUENCE__AUTH_TOKEN=..."
                ),
            ),
            FieldSpec(
                name="body_format",
                coercer=ChainCoercer(
                    Default("export_view"),
                    ParseString(),
                    OneOf("export_view", "view", "storage"),
                ),
                description=(
                    "Формат HTML-тела страницы из Confluence. "
                    "'export_view' — самый чистый для индексации. "
                    "'storage' — с ac:* macros (для structural parse)."
                ),
            ),
            FieldSpec(
                name="timeout_sec",
                coercer=ChainCoercer(
                    Default(30.0), ParseFloat(), MinValue(1.0)
                ),
                description="HTTP timeout per request, секунды.",
            ),
        ],
        factory=ConfluenceCommonConfig,
    )


def build_auth(cfg: ConfluenceCommonConfig) -> ConfluenceAuth:
    """ConfluenceCommonConfig → ConfluenceAuth.

    Бросает AuthError, если поля пустые или несовместимы.
    """
    if not cfg.auth_token:
        msg = (
            "auth_token пустой. Задай его через env: "
            "BOBA_INDEXER__SOURCES__CONFLUENCE__AUTH_TOKEN=..."
        )
        raise AuthError(msg)
    if cfg.auth_method == "pat":
        return PatAuth(token=cfg.auth_token)
    if cfg.auth_method == "basic":
        if not cfg.auth_user:
            msg = "auth_method='basic' требует auth_user."
            raise AuthError(msg)
        return BasicAuth(user=cfg.auth_user, password=cfg.auth_token)
    msg = f"unknown auth_method: {cfg.auth_method!r}"
    raise AuthError(msg)
