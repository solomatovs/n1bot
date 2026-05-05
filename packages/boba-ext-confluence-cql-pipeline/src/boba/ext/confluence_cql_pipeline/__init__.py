"""boba-ext-confluence-cql-pipeline: индексация по CQL-запросу."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.confluence_cql_pipeline.config import (
    ConfluenceCqlPipelineConfig,
    ConfluenceCqlPipelineConfigSection,
)
from boba.ext.confluence_cql_pipeline.factory import (
    ConfluenceCqlPipelineFactory,
)
from boba.indexing import IndexerExtensionContext, PipelineFactory

__all__ = [
    "ConfluenceCqlPipelineConfig",
    "ConfluenceCqlPipelineConfigSection",
    "ConfluenceCqlPipelineFactory",
    "register_pipelines",
]


def register_pipelines(
    ctx: IndexerExtensionContext,
) -> Iterable[PipelineFactory]:
    del ctx
    yield ConfluenceCqlPipelineFactory()
