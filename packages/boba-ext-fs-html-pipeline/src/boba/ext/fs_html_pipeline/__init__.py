"""boba-ext-fs-html-pipeline: индексация .html из FS."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.fs_html_pipeline.config import (
    FsHtmlPipelineConfig,
    FsHtmlPipelineConfigSection,
)
from boba.ext.fs_html_pipeline.factory import FsHtmlPipelineFactory
from boba.indexing import IndexerExtensionContext, PipelineFactory

__all__ = [
    "FsHtmlPipelineConfig",
    "FsHtmlPipelineConfigSection",
    "FsHtmlPipelineFactory",
    "register_pipelines",
]


def register_pipelines(
    ctx: IndexerExtensionContext,
) -> Iterable[PipelineFactory]:
    del ctx
    yield FsHtmlPipelineFactory()
