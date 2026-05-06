"""Pipeline-плагин: индексация всего Confluence space.

ConfigSection [indexer.pipelines.confluence_space] + PIPELINE через factory.
"""

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
from boba.confluence import ConfluenceConnection
from boba.declaration import FieldSpec, ObjectSchema
from boba.confluence_pipelines.pipeline_factory import (
    ConfluenceIndexingPipelineFactory,
)
from boba.confluence_pipelines.request_sources import (
    ConfluenceSpaceRequestSource,
)

__all__ = [
    "PIPELINE",
    "ConfluenceSpacePipelineConfig",
    "ConfluenceSpacePipelineConfigSection",
]


@dataclass(frozen=True)
class ConfluenceSpacePipelineConfig:
    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    space_key: str
    body_format: str = "export_view"
    chunk_size: int = 1500
    chunk_overlap: int = 150


class ConfluenceSpacePipelineConfigSection(
    ConfigSection[ConfluenceSpacePipelineConfig]
):
    """Pipeline индексации целого Confluence space (REST + heading chunker)."""

    namespace: ClassVar[tuple[str, ...]] = (
        "indexer",
        "pipelines",
        "confluence_space",
    )

    schema: ClassVar[ObjectSchema[ConfluenceSpacePipelineConfig]] = ObjectSchema(
        fields=[
            *ConfluenceConnection.fields(),
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
        factory=ConfluenceSpacePipelineConfig,
    )


PIPELINE = ConfluenceIndexingPipelineFactory.make(
    ConfluenceSpacePipelineConfigSection,
    lambda cfg: ConfluenceSpaceRequestSource(
        base_url=cfg.base_url,
        auth=ConfluenceConnection.make_auth(cfg),
        space_key=cfg.space_key,
        body_format=cfg.body_format,
        timeout_sec=cfg.timeout_sec,
    ),
)
