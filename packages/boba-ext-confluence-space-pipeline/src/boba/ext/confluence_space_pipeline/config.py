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
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = [
    "ConfluenceSpacePipelineConfig",
    "ConfluenceSpacePipelineConfigSection",
]


@dataclass(frozen=True)
class ConfluenceSpacePipelineConfig:
    base_url: str = ""
    auth_method: str = "pat"
    auth_user: str = ""
    auth_token: str = ""
    space_key: str = ""
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
        description=(
            "Pipeline 'ext.confluence_space': REST `/rest/api/space/<key>/content`"
            " → HttpTransport → ConfluenceReader → heading chunker → ChromaDB."
            " Чувствительные поля (base_url/auth_token) задаются env, не TOML."
        ),
        fields=[
            FieldSpec(
                name="base_url",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "URL Confluence Server, например `https://confl.x.com`. "
                    "Задаётся env: BOBA_INDEXER__PIPELINES__CONFLUENCE_SPACE__BASE_URL."
                ),
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
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "PAT или пароль; задаётся env: "
                    "BOBA_INDEXER__PIPELINES__CONFLUENCE_SPACE__AUTH_TOKEN."
                ),
            ),
            FieldSpec(
                name="space_key",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "Ключ space'а (виден в URL `/display/<KEY>/...`)."
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
                    "Формат тела страницы: `export_view` (рекомендуется), "
                    "`view`, `storage`."
                ),
            ),
            FieldSpec(
                name="timeout_sec",
                coercer=ChainCoercer(Default(30.0), ParseFloat()),
                description="HTTP-таймаут (сек) для discovery + fetch.",
            ),
            FieldSpec(
                name="chunk_size",
                coercer=ChainCoercer(Default(1500), ParseInt(), MinValue(1)),
                description="Целевой размер чанка (символов).",
            ),
            FieldSpec(
                name="chunk_overlap",
                coercer=ChainCoercer(Default(150), ParseInt()),
                description="Перекрытие sub-чанков внутри одной Section.",
            ),
        ],
        factory=ConfluenceSpacePipelineConfig,
    )
