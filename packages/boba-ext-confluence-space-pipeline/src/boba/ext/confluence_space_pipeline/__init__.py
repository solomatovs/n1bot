"""boba-ext-confluence-space-pipeline: индексация целого Confluence space."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.confluence_space_pipeline.config import (
    ConfluenceSpacePipelineConfig,
    ConfluenceSpacePipelineConfigSection,
)
from boba.ext.confluence_space_pipeline.factory import (
    ConfluenceSpacePipelineFactory,
)
from boba.indexing import IndexerExtensionContext, PipelineFactory

__all__ = [
    "ConfluenceSpacePipelineConfig",
    "ConfluenceSpacePipelineConfigSection",
    "ConfluenceSpacePipelineFactory",
    "register_pipelines",
]


def register_pipelines(
    ctx: IndexerExtensionContext,
) -> Iterable[PipelineFactory]:
    """Entry-point boba.indexing.pipelines."""
    del ctx
    yield ConfluenceSpacePipelineFactory()
