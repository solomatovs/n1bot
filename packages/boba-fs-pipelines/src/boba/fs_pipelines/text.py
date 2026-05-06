"""Pipeline: FsWalk → FsTransport → TextReader → SlidingChunker → ChromadbStore."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.chromadb_store import ChromadbPersistStore
from boba.chunking.sliding import SlidingChunker, SlidingChunkerConfig
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
from boba.ext.chromadb_shared import ChromadbSharedSection, make_embedding_function
from boba.fs_pipelines.text_reader import TextReader
from boba.fs_pipelines.fs_transport import FsTransport, FsWalkRequestSource
from boba.indexing import IndexPipeline, PipelineSpec

__all__ = [
    "PIPELINE",
    "FsTextPipelineConfig",
    "FsTextPipelineConfigSection",
]


@dataclass(frozen=True)
class FsTextPipelineConfig:
    paths: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    follow_symlinks: bool = False
    chunk_size: int = 1000
    chunk_overlap: int = 200


class FsTextPipelineConfigSection(ConfigSection[FsTextPipelineConfig]):
    """Pipeline FS-обхода + TextReader (plain UTF-8) + sliding chunker."""

    namespace: ClassVar[tuple[str, ...]] = ("indexer", "pipelines", "fs_text")

    schema: ClassVar[ObjectSchema[FsTextPipelineConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="paths",
                coercer=ChainCoercer(ParseCsvList(), NonEmpty()),
                required=True,
                description="Файлы и/или директории для обхода.",
            ),
            FieldSpec(
                name="include",
                coercer=ParseCsvList(),
                description="Glob-паттерны включения, например '*.txt,*.log'.",
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
                coercer=ChainCoercer(Default(1000), ParseInt(), MinValue(1)),
                description="Размер чанка sliding-window (символов).",
            ),
            FieldSpec(
                name="chunk_overlap",
                coercer=ChainCoercer(Default(200), ParseInt()),
                description="Перекрытие соседних чанков.",
            ),
        ],
        factory=FsTextPipelineConfig,
    )


def _build(app: AppConfig) -> IndexPipeline:
    cfg = app.section(FsTextPipelineConfigSection)
    shared = app.section(ChromadbSharedSection)
    return IndexPipeline(
        request_source=FsWalkRequestSource(
            paths=cfg.paths,
            include=cfg.include,
            exclude=cfg.exclude,
            follow_symlinks=cfg.follow_symlinks,
        ),
        transport=FsTransport(),
        reader=TextReader(),
        chunker=SlidingChunker(
            SlidingChunkerConfig(
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
    section=FsTextPipelineConfigSection(),
    build=_build,
)
