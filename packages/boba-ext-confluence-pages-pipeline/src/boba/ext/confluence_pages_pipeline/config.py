"""ConfigSection [indexer.pipelines.confluence_pages]."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    NonEmpty,
    OneOf,
    ParseCsvList,
    ParseInt,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.confluence_shared import ConfluenceConnection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = [
    "ConfluencePagesPipelineConfig",
    "ConfluencePagesPipelineConfigSection",
]


@dataclass(frozen=True)
class ConfluencePagesPipelineConfig:
    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    page_ids: list[str] = field(default_factory=list)
    body_format: str = "export_view"
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
        fields=[
            *ConfluenceConnection.fields(),
            FieldSpec(
                name="page_ids",
                coercer=ChainCoercer(ParseCsvList(), NonEmpty()),
                required=True,
                description="Page-id'ы через запятую.",
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
        invariants=ConfluenceConnection.invariant(),
        factory=ConfluencePagesPipelineConfig,
    )
