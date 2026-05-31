"""LlmMetadataChunker: проекция native-ключей → унифицированные `llm.*`."""

from __future__ import annotations

from collections.abc import Iterable

from boba.indexing import (
    BytesContentHash,
    Chunk,
    Chunker,
    ChunkerId,
    ChunkId,
    Metadata,
    PipelineContext,
    ReaderKeys,
    Section,
    SectionKeys,
    SourceId,
)
from boba.indexing.context import PipelineId
from boba.kbdoc import KbDocKeys
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.core.llm_keys import LlmKeys
from boba.tool.kb.core.llm_metadata_chunker import LlmMetadataChunker

_CTX = PipelineContext(pipeline_id=PipelineId("t"))


class _FakeChunker(Chunker[str]):
    def __init__(self, chunks: list[Chunk[str]]) -> None:
        self._chunks = chunks

    def name(self) -> str:
        return "fake"

    def chunker_id(self) -> ChunkerId:
        return ChunkerId("fake")

    def stream(
        self,
        ctx: PipelineContext,
        stream: Iterable[Section[str]],
    ) -> Iterable[Chunk[str]]:
        del ctx, stream
        yield from self._chunks


def _chunk(*, source_id: str, metadata: Metadata, tags: frozenset[str]) -> Chunk[str]:
    return Chunk(
        chunk_id=ChunkId("d:0"),
        source_id=SourceId(source_id),
        format_content="c",
        raw_content="c",
        chunk_index=0,
        content_hash=BytesContentHash(raw=b"\x00"),
        metadata=metadata,
        tags=tags,
    )


def _run(chunk: Chunk[str]) -> Chunk[str]:
    out = list(LlmMetadataChunker(_FakeChunker([chunk])).stream(_CTX, []))
    assert len(out) == 1
    return out[0]


def test_kbdoc_chunk_uses_source_url() -> None:
    meta = (
        Metadata.empty()
        .set(ReaderKeys.PAGE_TITLE, "Правила именования")
        .set(KbDocKeys.SOURCE_URL, "https://confl/viewpage?pageId=950276")
        .set(KbDocKeys.PAGE_ID, "950276")
        .set(KbDocKeys.SPACE, "PAAS")
        .set(SectionKeys.HEADING_PATH, "Backup › PITR")
    )
    result = _run(
        _chunk(
            source_id="ws:sess:upload/x.md",
            metadata=meta,
            tags=frozenset({"b", "a"}),
        )
    )
    m = result.metadata
    assert m.get(LlmKeys.PAGE_TITLE) == "Правила именования"
    assert m.get(LlmKeys.SOURCE) == "https://confl/viewpage?pageId=950276"
    assert m.get(LlmKeys.PAGE_ID) == "950276"
    assert m.get(LlmKeys.LOCATION) == "Backup › PITR"
    assert m.get(LlmKeys.SPACE) == "PAAS"
    assert m.get(LlmKeys.TAGS) == "a, b"  # стабильный порядок


def test_confluence_chunk_falls_back_to_source_id() -> None:
    url = "https://confl/pages/viewpage.action?pageId=950276"
    meta = (
        Metadata.empty()
        .set(ReaderKeys.PAGE_TITLE, "Postgres Runbook")
        .set(ConfluenceKeys.PAGE_ID, "950276")
        .set(ConfluenceKeys.SPACE_KEY, "DOCS")
        .set(SectionKeys.HEADING_PATH, "Backup › PITR")
    )
    result = _run(_chunk(source_id=url, metadata=meta, tags=frozenset()))
    m = result.metadata
    assert m.get(LlmKeys.SOURCE) == url  # нет source_url → source_id (это URL)
    assert m.get(LlmKeys.PAGE_ID) == "950276"  # confluence.page_id
    assert m.get(LlmKeys.SPACE) == "DOCS"
    assert m.get(LlmKeys.TAGS) is None  # пустые tags не проставляются


def test_anchor_appended_to_source() -> None:
    url = "https://confl/pages/viewpage.action?pageId=950276"
    meta = (
        Metadata.empty()
        .set(ConfluenceKeys.PAGE_ID, "950276")
        .set(SectionKeys.ANCHOR, "backup-pitr")
    )
    result = _run(_chunk(source_id=url, metadata=meta, tags=frozenset()))
    assert result.metadata.get(LlmKeys.SOURCE) == f"{url}#backup-pitr"


def test_anchor_not_duplicated_if_source_has_fragment() -> None:
    meta = (
        Metadata.empty()
        .set(KbDocKeys.SOURCE_URL, "https://x#existing")
        .set(SectionKeys.ANCHOR, "backup-pitr")
    )
    result = _run(_chunk(source_id="ws:x", metadata=meta, tags=frozenset()))
    assert result.metadata.get(LlmKeys.SOURCE) == "https://x#existing"


def test_native_keys_preserved() -> None:
    meta = Metadata.empty().set(KbDocKeys.SOURCE_URL, "https://x")
    result = _run(_chunk(source_id="ws:x", metadata=meta, tags=frozenset()))
    # native-ключ на месте, llm.* добавлен поверх
    assert result.metadata.get(KbDocKeys.SOURCE_URL) == "https://x"
    assert result.metadata.get(LlmKeys.SOURCE) == "https://x"
