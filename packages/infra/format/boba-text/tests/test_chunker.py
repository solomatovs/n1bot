"""SectionChunker: пробрасывает Section.to_chunk_metadata() в Chunk.metadata."""

from __future__ import annotations

from collections.abc import Iterable

from boba.indexing import (
    ChunkerId,
    ChunkId,
    ChunkIdGenerator,
    ChunkLocation,
    HeadingSection,
    Metadata,
    PipelineContext,
    PipelineId,
    Section,
    SectionKeys,
    Sha256TextEncoder,
    SourceId,
    SplitPiece,
    Splitter,
)
from boba.text.chunker import SectionChunker


class _IdentitySplitter(Splitter[str]):
    """Один piece на всю секцию — изолируем тест от splitter-логики."""

    def split(self, value: str) -> Iterable[SplitPiece[str]]:
        yield SplitPiece(content=value, location=ChunkLocation(start=0, end=len(value)))


class _StaticIdGenerator(ChunkIdGenerator[str]):
    def compute(self, section: Section[str], chunk_index: int) -> ChunkId:
        del section
        return ChunkId(f"static:{chunk_index}")


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("test"))


def test_to_chunk_metadata_is_merged_into_chunk():
    """HeadingSection кладёт HEADING_LEVEL/TEXT через to_chunk_metadata."""
    section = HeadingSection(
        source_id=SourceId("doc1"),
        content="<h1>Intro</h1>",
        order=0,
        level=1,
        text="Intro",
    )
    chunker = SectionChunker(
        chunker_id=ChunkerId("test"),
        splitter=_IdentitySplitter(),
        id_strategy=_StaticIdGenerator(),
        content_hasher=Sha256TextEncoder(),
    )
    [chunk] = list(chunker.stream(_ctx(), iter([section])))
    assert chunk.metadata.get(SectionKeys.HEADING_LEVEL) == 1
    assert chunk.metadata.get(SectionKeys.HEADING_TEXT) == "Intro"


def test_section_metadata_overrides_typed_keys_on_collision():
    """Section.metadata.merge(to_chunk_metadata()): другой побеждает.

    Контракт `Metadata.merge`: правый аргумент перекрывает левый. Значит
    typed-keys из to_chunk_metadata() побеждают пользовательскую metadata
    при коллизии — это правильно: Section знает свои поля точнее.
    """
    upstream = Metadata.empty().set(SectionKeys.HEADING_LEVEL, 99)
    section = HeadingSection(
        source_id=SourceId("doc1"),
        content="<h1>X</h1>",
        order=0,
        metadata=upstream,
        level=2,
        text="X",
    )
    chunker = SectionChunker(
        chunker_id=ChunkerId("test"),
        splitter=_IdentitySplitter(),
        id_strategy=_StaticIdGenerator(),
        content_hasher=Sha256TextEncoder(),
    )
    [chunk] = list(chunker.stream(_ctx(), iter([section])))
    assert chunk.metadata.get(SectionKeys.HEADING_LEVEL) == 2
