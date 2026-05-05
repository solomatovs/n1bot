"""boba-ext-confluence-pages-pipeline: индексация явного списка page-id'ов."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.confluence_pages_pipeline.config import (
    ConfluencePagesPipelineConfig,
    ConfluencePagesPipelineConfigSection,
)
from boba.ext.confluence_pages_pipeline.factory import (
    ConfluencePagesPipelineFactory,
)
from boba.indexing import IndexerExtensionContext, PipelineFactory

__all__ = [
    "ConfluencePagesPipelineConfig",
    "ConfluencePagesPipelineConfigSection",
    "ConfluencePagesPipelineFactory",
    "register_pipelines",
]


def register_pipelines(
    ctx: IndexerExtensionContext,
) -> Iterable[PipelineFactory]:
    del ctx
    yield ConfluencePagesPipelineFactory()
