"""Pipeline: FsWalk → FsTransport → HtmlReader → HeadingChunker → ChromadbStore."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.chromadb_store import ChromadbPersistStore
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
from boba.fs_transport import FsTransport, FsWalkRequestSource
from boba.chunking.heading import HeadingChunker, HeadingChunkerConfig
from boba.html_reader import HtmlReader
from boba.indexing import IndexPipeline, PipelineSpec

__all__ = [
    "PIPELINE",
    "FsHtmlPipelineConfig",
    "FsHtmlPipelineConfigSection",
]


@dataclass(frozen=True)
class FsHtmlPipelineConfig:
    paths: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    follow_symlinks: bool = False
    chunk_size: int = 1500
    chunk_overlap: int = 150


class FsHtmlPipelineConfigSection(ConfigSection[FsHtmlPipelineConfig]):
    """Pipeline FS-обхода + HtmlReader + heading chunker."""

    namespace: ClassVar[tuple[str, ...]] = ("indexer", "pipelines", "fs_html")

    schema: ClassVar[ObjectSchema[FsHtmlPipelineConfig]] = ObjectSchema(
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
                description="Glob-паттерны включения, например '*.html,*.htm'.",
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
                description="Перекрытие sub-чанков.",
            ),
        ],
        factory=FsHtmlPipelineConfig,
    )


def _build(app: AppConfig) -> IndexPipeline:
    cfg = app.section(FsHtmlPipelineConfigSection)
    shared = app.section(ChromadbSharedSection)
    return IndexPipeline(
        request_source=FsWalkRequestSource(
            paths=cfg.paths,
            include=cfg.include,
            exclude=cfg.exclude,
            follow_symlinks=cfg.follow_symlinks,
        ),
        transport=FsTransport(),
        reader=HtmlReader(),
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
    section=FsHtmlPipelineConfigSection(),
    build=_build,
)
