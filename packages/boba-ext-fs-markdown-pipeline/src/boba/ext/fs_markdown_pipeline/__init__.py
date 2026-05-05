"""boba-ext-fs-markdown-pipeline: pipeline-плагин для индексации .md из FS."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.fs_markdown_pipeline.config import (
    FsMarkdownPipelineConfig,
    FsMarkdownPipelineConfigSection,
)
from boba.ext.fs_markdown_pipeline.factory import FsMarkdownPipelineFactory
from boba.indexing import IndexerExtensionContext, PipelineFactory

__all__ = [
    "FsMarkdownPipelineConfig",
    "FsMarkdownPipelineConfigSection",
    "FsMarkdownPipelineFactory",
    "register_pipelines",
]


def register_pipelines(
    ctx: IndexerExtensionContext,
) -> Iterable[PipelineFactory]:
    """Entry-point boba.indexing.pipelines."""
    del ctx
    yield FsMarkdownPipelineFactory()
