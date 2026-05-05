"""ConfigSection [indexer.pipelines.confluence_cql]."""

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
    "ConfluenceCqlPipelineConfig",
    "ConfluenceCqlPipelineConfigSection",
]


@dataclass(frozen=True)
class ConfluenceCqlPipelineConfig:
    base_url: str = ""
    auth_method: str = "pat"
    auth_user: str = ""
    auth_token: str = ""
    cql: str = ""
    body_format: str = "export_view"
    timeout_sec: float = 30.0
    chunk_size: int = 1500
    chunk_overlap: int = 150


class ConfluenceCqlPipelineConfigSection(
    ConfigSection[ConfluenceCqlPipelineConfig]
):
    """Pipeline индексации по CQL (Confluence Query Language)."""

    namespace: ClassVar[tuple[str, ...]] = (
        "indexer", "pipelines", "confluence_cql",
    )

    schema: ClassVar[ObjectSchema[ConfluenceCqlPipelineConfig]] = ObjectSchema(
        description=(
            "Pipeline 'ext.confluence_cql': произвольный CQL-запрос → "
            "REST `/rest/api/content/search?cql=...` → ConfluenceReader → "
            "heading chunker → ChromaDB."
        ),
        fields=[
            FieldSpec(
                name="base_url",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "URL Confluence (env: "
                    "BOBA_INDEXER__PIPELINES__CONFLUENCE_CQL__BASE_URL)."
                ),
            ),
            FieldSpec(
                name="auth_method",
                coercer=ChainCoercer(
                    Default("pat"), ParseString(), OneOf("pat", "basic"),
                ),
                description="`pat` — Bearer; `basic` — login+password.",
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
                    "Токен (env: "
                    "BOBA_INDEXER__PIPELINES__CONFLUENCE_CQL__AUTH_TOKEN)."
                ),
            ),
            FieldSpec(
                name="cql",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "CQL-запрос. Примеры: "
                    "'space = DOCS AND ancestor = 12345' (поддерево), "
                    "'label = api AND space = DOCS' (по тегу), "
                    "'space = DOCS AND lastModified > \\'2024-01-01\\''."
                ),
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
                description="Перекрытие sub-чанков внутри одной Section.",
            ),
        ],
        factory=ConfluenceCqlPipelineConfig,
    )
