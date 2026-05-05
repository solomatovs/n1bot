"""boba-ext-fs-text-pipeline: индексация .txt/.log из FS."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.fs_text_pipeline.config import (
    FsTextPipelineConfig,
    FsTextPipelineConfigSection,
)
from boba.ext.fs_text_pipeline.factory import FsTextPipelineFactory
from boba.indexing import IndexerExtensionContext, PipelineFactory

__all__ = [
    "FsTextPipelineConfig",
    "FsTextPipelineConfigSection",
    "FsTextPipelineFactory",
    "register_pipelines",
]


def register_pipelines(
    ctx: IndexerExtensionContext,
) -> Iterable[PipelineFactory]:
    del ctx
    yield FsTextPipelineFactory()
