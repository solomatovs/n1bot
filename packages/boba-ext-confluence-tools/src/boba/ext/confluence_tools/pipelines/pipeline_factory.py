"""Общая factory для 3 indexing-pipeline (cql / pages / space).

Все три pipeline отличаются только типом RequestSource и набором полей
конфига; остальные стадии (transport / decoder / reader / chunker / store) —
идентичны. Factory принимает ConfigSection-класс и фабрику RequestSource,
возвращает готовый PipelineSpec.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from boba.chromadb_store import ChromadbPersistStore
from boba.chunking.heading import HeadingChunker, HeadingChunkerConfig
from boba.config.app import AppConfig
from boba.config.section import ConfigSection
from boba.ext.chromadb_shared import ChromadbSharedSection, make_embedding_function
from boba.ext.confluence_tools.connection import (
    ConfluenceConnection,
    ConfluenceConnectionConfig,
)
from boba.ext.confluence_tools.decoder import ConfluenceJsonDecoder
from boba.ext.confluence_tools.reader import ConfluenceReader
from boba.http_transport import HttpRequest
from boba.indexing import IndexPipeline, PipelineSpec
from boba.processing import RequestSource

__all__ = ["ConfluenceIndexingPipelineFactory"]


class _IndexingPipelineConfig(ConfluenceConnectionConfig, Protocol):
    """Контракт config'а для indexing-pipeline:
    connection + body_format + chunk-параметры.
    """

    @property
    def body_format(self) -> str: ...
    @property
    def chunk_size(self) -> int: ...
    @property
    def chunk_overlap(self) -> int: ...


_T = TypeVar("_T", bound=_IndexingPipelineConfig)


# PipelineFactory для Confluence indexing-pipeline'ов. Собирает конвейер из
class ConfluenceIndexingPipelineFactory:
    """Factory для Confluence indexing-pipeline'ов."""

    @staticmethod
    def make(
        section_cls: type[ConfigSection[_T]],
        request_source: Callable[[_T], RequestSource[HttpRequest]],
    ) -> PipelineSpec:
        """Собирает PipelineSpec из ConfigSection-класса и фабрики RequestSource.

        `request_source` принимает уже-загруженный config (T) и строит нужный
        RequestSource (Cql / Pages / Space). Остальные стадии одинаковые.
        """

        def _build(app: AppConfig) -> IndexPipeline:
            cfg = app.section(section_cls)
            shared = app.section(ChromadbSharedSection)
            return IndexPipeline(
                request_source=request_source(cfg),
                transport=ConfluenceConnection.make_transport(cfg),
                decoder=ConfluenceJsonDecoder(body_format=cfg.body_format),
                reader=ConfluenceReader(),
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

        return PipelineSpec(section=section_cls(), build=_build)
