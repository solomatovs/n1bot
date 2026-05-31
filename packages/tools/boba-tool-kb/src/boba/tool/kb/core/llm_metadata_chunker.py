"""LlmMetadataChunker — обёртка `Chunker[str]`, проецирующая native-ключи в `llm.*`.

Делегирует нарезку inner-чанкеру, а на каждом готовом чанке выводит
source-агностичный набор `llm.*` (см. `LlmKeys`) из тех native-ключей, что
уже лежат в `chunk.metadata` / `chunk.tags` к моменту эмиссии. Подключается
в KB-ingest'ах (kbdoc + confluence) — одно место, единый контракт выдачи для
LLM независимо от источника.

Маппинг (значение ставится только если непустое):

    llm.page_title ← reader.page_title
    llm.source     ← (source_url ?? chunk.source_id) + `#section.anchor` если есть
    llm.page_id    ← confluence.page_id ?? reader.kbdoc.page_id
    llm.location   ← section.heading.path
    llm.tags       ← chunk.tags                       ("tag1, tag2")
    llm.space      ← confluence.space_key ?? reader.kbdoc.space
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    PipelineContext,
    ReaderKeys,
    Section,
    SectionKeys,
)
from boba.indexing.chunks import ChunkKeys
from boba.kbdoc import KbDocKeys
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.core.llm_keys import LlmKeys

__all__ = ["LlmMetadataChunker"]


class LlmMetadataChunker(Chunker[str]):
    """Декоратор `Chunker[str]`: обогащает чанки унифицированными `llm.*`."""

    def __init__(self, inner: Chunker[str]) -> None:
        self._inner = inner

    def name(self) -> str:
        return f"LlmMetadataChunker({self._inner.name()})"

    def chunker_id(self) -> ChunkerId:
        return self._inner.chunker_id()

    def reset(self) -> None:
        self._inner.reset()

    def stream(
        self,
        ctx: PipelineContext,
        stream: Iterable[Section[str]],
    ) -> Iterable[Chunk[str]]:
        for chunk in self._inner.stream(ctx, stream):
            yield self._enrich(chunk)

    @staticmethod
    def _enrich(chunk: Chunk[str]) -> Chunk[str]:
        meta = chunk.metadata

        title = meta.get(ReaderKeys.PAGE_TITLE)
        source = meta.get(KbDocKeys.SOURCE_URL) or str(chunk.source_id)
        anchor = meta.get(SectionKeys.ANCHOR) or meta.get(ChunkKeys.ANCHOR)
        if anchor and "#" not in source:
            source = f"{source}#{anchor}"
        page_id = meta.get(ConfluenceKeys.PAGE_ID) or meta.get(KbDocKeys.PAGE_ID)
        location = meta.get(SectionKeys.HEADING_PATH)
        space = meta.get(ConfluenceKeys.SPACE_KEY) or meta.get(KbDocKeys.SPACE)
        tags = ", ".join(sorted(chunk.tags)) if chunk.tags else None

        new_meta = meta
        if title:
            new_meta = new_meta.set(LlmKeys.PAGE_TITLE, title)
        if source:
            new_meta = new_meta.set(LlmKeys.SOURCE, source)
        if page_id:
            new_meta = new_meta.set(LlmKeys.PAGE_ID, page_id)
        if location:
            new_meta = new_meta.set(LlmKeys.LOCATION, location)
        if tags:
            new_meta = new_meta.set(LlmKeys.TAGS, tags)
        if space:
            new_meta = new_meta.set(LlmKeys.SPACE, space)

        if new_meta is meta:
            return chunk
        return replace(chunk, metadata=new_meta)
