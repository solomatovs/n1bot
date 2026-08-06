"""Разбор источника: секции документа, сырой документ и его декодер."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import ClassVar, Generic, NewType, Protocol, TypeVar

from boba.indexing.values import (
    ChunkLocation,
    FormatBlock,
    FormatPlan,
    Metadata,
    MetadataKey,
)

__all__ = [
    "AsyncBinaryStream",
    "ChunkStream",
    "Decoder",
    "DecoderId",
    "HeadingSection",
    "ParagraphSection",
    "RawDocument",
    "Section",
    "SectionKeys",
    "SourceId",
]

T = TypeVar("T")


SourceId = NewType("SourceId", str)
"""Стабильный canonical id документа-источника (URL, fs-path, doc-key)."""


class SectionKeys:
    """Стандартные MetadataKey-и для атрибутов доменных Section'ов.

    LOCATION_START / LOCATION_END / ANCHOR — координаты/идентификаторы;
    парсеры пишут их в section.metadata если умеют, для других форматов
    отсутствуют.

    HEADING_LEVEL / HEADING_TEXT — структурные поля HeadingSection,
    эмитятся через to_chunk_metadata().

    Format-specific подклассы (в format-пакетах) определяют свои
    собственные *Keys-классы рядом со своими Section-типами.
    """

    LOCATION_START: ClassVar[MetadataKey[int]] = MetadataKey(
        name="section.location.start",
        decode=int,
        encode=str,
    )
    LOCATION_END: ClassVar[MetadataKey[int]] = MetadataKey(
        name="section.location.end",
        decode=int,
        encode=str,
    )
    ANCHOR: ClassVar[MetadataKey[str]] = MetadataKey(
        name="section.anchor",
        decode=str,
        encode=str,
    )
    HEADING_LEVEL: ClassVar[MetadataKey[int]] = MetadataKey(
        name="section.heading.level",
        decode=int,
        encode=str,
    )
    HEADING_TEXT: ClassVar[MetadataKey[str]] = MetadataKey(
        name="section.heading.text",
        decode=str,
        encode=str,
    )
    HEADING_PATH: ClassVar[MetadataKey[str]] = MetadataKey(
        name="section.heading.path",
        decode=str,
        encode=str,
    )
    PAGE_NUMBER: ClassVar[MetadataKey[int]] = MetadataKey(
        name="section.page_number",
        decode=int,
        encode=str,
    )
    """Номер страницы/листа источника (1-based) — локус цитирования для
    постранично читаемых форматов (PDF/docx/xlsx). Для форматов без
    страниц отсутствует."""


@dataclass(frozen=True)
class Section(Generic[T]):
    """Логический фрагмент документа. Базовый класс открытой иерархии.

    Подклассы добавляют типизированные структурные поля и переопределяют
    to_chunk_metadata() для эмиссии этих полей в chunk.metadata.

    Поля:

    - source_id — id source-документа.
    - content   — текст/bytes раздела (для текстовых форматов — T = str).
    - order     — порядок секции в исходном документе (детерминирует chunk_id).
    - metadata  — пробрасываемая metadata. Сюда же парсер кладёт
                     координаты/идентификаторы через SectionKeys.LOCATION_*
                     и SectionKeys.ANCHOR, если умеет их вычислить.
    - tags      — множество тэгов.
    """

    SECTION_TYPE: ClassVar[str] = "section"

    source_id: SourceId
    content: T
    order: int = 0
    metadata: Metadata = field(default_factory=Metadata.empty)
    tags: frozenset[str] = field(default_factory=frozenset)

    def to_chunk_metadata(self) -> Metadata:
        """Типизированные структурные поля Section -> chunk.metadata.

        Базовый Section не несёт типизированных полей — возвращает
        Metadata.empty(). Подклассы переопределяют.
        """
        return Metadata.empty()

    def to_format_plan(self) -> FormatPlan:
        """План рендера секции в LLM-формат для format-aware chunker'а.

        Дефолт — один не-atomic блок с format_content == raw_content == content.
        Format-specific подклассы переопределяют, чтобы отдать markdown-render,
        per-unit raw, replicate-header и breadcrumb-info.
        """
        body = str(self.content)
        return FormatPlan(
            blocks=(
                FormatBlock(
                    format_content=body,
                    raw_content=body,
                    location=ChunkLocation(start=0, end=len(body)),
                ),
            ),
        )


@dataclass(frozen=True)
class HeadingSection(Section[str]):
    """Раздел с heading-маркером (# , <h1>, ...).

    content — оригинальный текст раздела с разметкой формата;
    level и text — разобранная типизированная информация.
    """

    SECTION_TYPE: ClassVar[str] = "heading"

    level: int = 1
    text: str = ""

    def to_chunk_metadata(self) -> Metadata:
        return (
            Metadata.empty()
            .set(SectionKeys.HEADING_LEVEL, self.level)
            .set(SectionKeys.HEADING_TEXT, self.text)
        )

    def to_format_plan(self) -> FormatPlan:
        """Markdown-heading + регистрация breadcrumb для chunker'а.

        Chunker по breadcrumb_level/breadcrumb_text обновит свой стек
        активных заголовков и пропишет полный путь в HEADING_PATH для
        последующих чанков того же source_id.
        """
        md = "#" * self.level + " " + self.text
        return FormatPlan(
            blocks=(
                FormatBlock(
                    format_content=md,
                    raw_content=str(self.content),
                    location=ChunkLocation(start=0, end=len(md)),
                    is_atomic=True,
                ),
            ),
            breadcrumb_level=self.level,
            breadcrumb_text=self.text,
        )


@dataclass(frozen=True)
class ParagraphSection(Section[str]):
    """Обычный текстовый параграф / fallback для не-типизированного контента.

    Inline-разметка остаётся в content (не разворачивается в подсекции).
    """

    SECTION_TYPE: ClassVar[str] = "paragraph"

class AsyncBinaryStream(Protocol):
    """Открытый async-поток тела: итерация чанками либо чтение целиком."""

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def read(self) -> bytes:
        """Дочитать остаток потока; для парсеров, которым нужен весь вход."""
        ...


class ChunkStream(AsyncBinaryStream):
    """AsyncBinaryStream поверх источника чанков; одноразовый, как и источник."""

    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._chunks

    async def read(self) -> bytes:
        parts: list[bytes] = []
        async for chunk in self._chunks:
            parts.append(chunk)
        return b"".join(parts)

    @classmethod
    def of(cls, payload: bytes) -> ChunkStream:
        """Поток из готового буфера — для стадий, которые сами собрали тело."""

        async def one() -> AsyncIterator[bytes]:
            yield payload

        return cls(one())


@dataclass(frozen=True)
class RawDocument:
    """Открытый поток тела + metadata; lifecycle потока — у Transport."""

    handle: AsyncBinaryStream
    """Открытый поток; Reader читает, закрывает его Transport."""

    source_id: SourceId
    """Identity документа; выводит и проставляет Transport (Transport.source_id)."""

    metadata: Metadata = field(default_factory=Metadata.empty)
    """Request.metadata + transport-specific keys; Reader/Chunker мержат свои ключи поверх."""

DecoderId = NewType("DecoderId", str)
"""Идентификатор Decoder-реализации."""


class Decoder(ABC):
    """RawDocument -> RawDocument: преобразование payload и/или metadata."""

    @abstractmethod
    def decoder_id(self) -> DecoderId: ...

    @abstractmethod
    async def decode(
        self,
        raw: RawDocument,
    ) -> RawDocument: ...
