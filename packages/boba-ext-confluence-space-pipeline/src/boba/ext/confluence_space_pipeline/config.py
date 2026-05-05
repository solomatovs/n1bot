"""ConfigSection [indexer.pipelines.confluence_space]."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    OneOf,
    ParseFloat,
    ParseInt,
    ParseString,
    RequiredWhen,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = [
    "ConfluenceSpacePipelineConfig",
    "ConfluenceSpacePipelineConfigSection",
]


@dataclass(frozen=True)
class ConfluenceSpacePipelineConfig:
    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    space_key: str
    body_format: str = "export_view"
    timeout_sec: float = 30.0
    chunk_size: int = 1500
    chunk_overlap: int = 150


class ConfluenceSpacePipelineConfigSection(
    ConfigSection[ConfluenceSpacePipelineConfig]
):
    """Pipeline индексации целого Confluence space (REST + heading chunker)."""

    namespace: ClassVar[tuple[str, ...]] = (
        "indexer", "pipelines", "confluence_space",
    )

    schema: ClassVar[ObjectSchema[ConfluenceSpacePipelineConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="base_url",
                coercer=ParseString(),
                required=True,
                description="URL Confluence Server (env: ...__BASE_URL).",
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
                name="space_key",
                coercer=ParseString(),
                required=True,
                description="Ключ space'а (виден в URL `/display/<KEY>/...`).",
            ),
            FieldSpec(
                name="body_format",
                coercer=ChainCoercer(
                    Default("export_view"),
                    ParseString(),
                    OneOf("export_view", "view", "storage"),
                ),
                description="Формат тела страницы.",
            ),
            FieldSpec(
                name="timeout_sec",
                coercer=ChainCoercer(Default(30.0), ParseFloat()),
                description="HTTP-таймаут (сек).",
            ),
            FieldSpec(
                name="chunk_size",
                coercer=ChainCoercer(Default(1500), ParseInt(), MinValue(1)),
                description="Целевой размер чанка (символов).",
            ),
            FieldSpec(
                name="chunk_overlap",
                coercer=ChainCoercer(Default(150), ParseInt()),
                description="Перекрытие sub-чанков.",
            ),
        ],
        invariants=RequiredWhen("auth_method", "basic", "auth_user"),
        factory=ConfluenceSpacePipelineConfig,
    )
