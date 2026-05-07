"""Pipeline: FsWalk → FsTransport → MarkdownReader → HeadingChunker → ChromadbStore."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.chunking.heading import HeadingChunker, HeadingChunkerConfig
from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    NonEmpty,
    ParseBool,
    ParseCsvList,
    ParseInt,
)
from boba.config.app import AppConfig
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.chromadb_tools.shared import (
    ChromadbSharedSection,
    make_embedding_function,
)
from boba.ext.chromadb_tools.store import ChromadbPersistStore
from boba.fs_pipelines.fs_transport import FsTransport, FsWalkRequestSource
from boba.fs_pipelines.markdown_reader import MarkdownReader
from boba.indexing import IndexPipeline, PipelineSpec

__all__ = [
    "PIPELINE",
    "FsMarkdownPipelineConfig",
    "FsMarkdownPipelineConfigSection",
]


@dataclass(frozen=True)
class FsMarkdownPipelineConfig:
    paths: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    follow_symlinks: bool = False
    chunk_size: int = 1500
    chunk_overlap: int = 150


class FsMarkdownPipelineConfigSection(ConfigSection[FsMarkdownPipelineConfig]):
    """Pipeline FS-обхода + MarkdownReader + heading chunker."""

    namespace: ClassVar[tuple[str, ...]] = ("indexer", "pipelines", "fs_markdown")

    schema: ClassVar[ObjectSchema[FsMarkdownPipelineConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="paths",
                coercer=ChainCoercer(ParseCsvList(), NonEmpty()),
                required=True,
                description="Файлы и/или директории для обхода (rglob).",
            ),
            FieldSpec(
                name="include",
                coercer=ParseCsvList(),
                description="Glob-паттерны включения. Пример: '*.md,*.markdown'.",
            ),
            FieldSpec(
                name="exclude",
                coercer=ParseCsvList(),
                description="Glob-паттерны исключения.",
            ),
            FieldSpec(
                name="follow_symlinks",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description="Следовать за symlink-ами.",
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
        factory=FsMarkdownPipelineConfig,
    )


def _build(app: AppConfig) -> IndexPipeline:
    cfg = app.section(FsMarkdownPipelineConfigSection)
    shared = app.section(ChromadbSharedSection)
    return IndexPipeline(
        request_source=FsWalkRequestSource(
            paths=cfg.paths,
            include=cfg.include,
            exclude=cfg.exclude,
            follow_symlinks=cfg.follow_symlinks,
        ),
        transport=FsTransport(),
        reader=MarkdownReader(),
        chunker=HeadingChunker(
            HeadingChunkerConfig(
                chunk_size=cfg.chunk_size,
                chunk_overlap=cfg.chunk_overlap,
            )
        ),
        store=ChromadbPersistStore(
            persist_path=shared.persist_path,
            embedding_function=make_embedding_function(shared),
        ),
    )


PIPELINE = PipelineSpec(
    section=FsMarkdownPipelineConfigSection(),
    build=_build,
)
