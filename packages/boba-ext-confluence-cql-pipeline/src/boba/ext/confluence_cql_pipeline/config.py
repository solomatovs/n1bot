"""ConfigSection [indexer.pipelines.confluence_cql]."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    OneOf,
    ParseInt,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.confluence_shared import ConfluenceConnection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = [
    "ConfluenceCqlPipelineConfig",
    "ConfluenceCqlPipelineConfigSection",
]


@dataclass(frozen=True)
class ConfluenceCqlPipelineConfig:
    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    cql: str
    body_format: str = "export_view"
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
        fields=[
            *ConfluenceConnection.fields(),
            FieldSpec(
                name="cql",
                coercer=ParseString(),
                required=True,
                description=(
                    "CQL-запрос. Примеры: 'space = DOCS AND ancestor = 12345', "
                    "'label = api AND space = DOCS'."
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
        factory=ConfluenceCqlPipelineConfig,
    )
