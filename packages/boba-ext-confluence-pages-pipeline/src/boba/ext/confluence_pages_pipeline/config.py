"""ConfigSection [indexer.pipelines.confluence_pages]."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    OneOf,
    ParseCsvList,
    ParseFloat,
    ParseInt,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = [
    "ConfluencePagesPipelineConfig",
    "ConfluencePagesPipelineConfigSection",
]


@dataclass(frozen=True)
class ConfluencePagesPipelineConfig:
    base_url: str = ""
    auth_method: str = "pat"
    auth_user: str = ""
    auth_token: str = ""
    page_ids: list[str] = field(default_factory=list)
    body_format: str = "export_view"
    timeout_sec: float = 30.0
    chunk_size: int = 1500
    chunk_overlap: int = 150


class ConfluencePagesPipelineConfigSection(
    ConfigSection[ConfluencePagesPipelineConfig]
):
    """Pipeline индексации явного списка Confluence-страниц."""

    namespace: ClassVar[tuple[str, ...]] = (
        "indexer", "pipelines", "confluence_pages",
    )

    schema: ClassVar[ObjectSchema[ConfluencePagesPipelineConfig]] = ObjectSchema(
        description=(
            "Pipeline 'ext.confluence_pages': явный список page-id'ов → "
            "REST `/rest/api/content/{id}` → ConfluenceReader → heading "
            "chunker → ChromaDB."
        ),
        fields=[
            FieldSpec(
                name="base_url",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "URL Confluence (env: "
                    "BOBA_INDEXER__PIPELINES__CONFLUENCE_PAGES__BASE_URL)."
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
                    "BOBA_INDEXER__PIPELINES__CONFLUENCE_PAGES__AUTH_TOKEN)."
                ),
            ),
            FieldSpec(
                name="page_ids",
                coercer=ParseCsvList(),
                description=(
                    "Page-id'ы через запятую (виден в URL Confluence: "
                    "`?pageId=12345`)."
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
        factory=ConfluencePagesPipelineConfig,
    )
