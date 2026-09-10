"""Разбор источника: секции документа, сырой документ и его декодер."""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
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
    "CardField",
    "ChunkStream",
    "Decoder",
    "DecoderId",
    "DocumentCardSection",
    "HeadingSection",
    "MarkdownTable",
    "MarkdownToken",
    "OutlineEntry",
    "ParagraphSection",
    "RawDocument",
    "Section",
    "SectionKeys",
    "SectionKind",
    "SourceId",
    "SpooledBody",
    "TableLayout",
    "TableSection",
    "TableToken",
]

T = TypeVar("T")


SourceId = NewType("SourceId", str)
"""Стабильный canonical id документа-источника (URL, fs-path, doc-key)."""


class SectionKind(StrEnum):
    """Вид секции; уходит в metadata чанка, поиск отбирает по нему.

    Карточка и таблица дают чанки, устроенные иначе, чем сплошной текст:
    карточка пересказывает документ, строка таблицы бессмысленна без шапки.
    Потребителю нужно уметь их различить, поэтому вид называется явно.
    """

    SECTION = "section"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    CARD = "card"


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

    @staticmethod
    def _decode_texts(raw: str) -> tuple[str, ...]:
        return tuple(str(item) for item in json.loads(raw))

    @staticmethod
    def _encode_texts(value: tuple[str, ...]) -> str:
        return json.dumps(list(value), ensure_ascii=False)

    KIND: ClassVar[MetadataKey[str]] = MetadataKey(
        name="section.kind",
        decode=str,
        encode=str,
    )
    """Вид секции (SectionKind), из которой собран чанк."""

    TABLE_CAPTION: ClassVar[MetadataKey[str]] = MetadataKey(
        name="section.table.caption",
        decode=str,
        encode=str,
    )

    TABLE_COLUMNS: ClassVar[MetadataKey[tuple[str, ...]]] = MetadataKey(
        name="section.table.columns",
        decode=_decode_texts,
        encode=_encode_texts,
    )


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

    SECTION_TYPE: ClassVar[SectionKind] = SectionKind.SECTION

    source_id: SourceId
    content: T
    order: int = 0
    metadata: Metadata = field(default_factory=Metadata.empty)
    tags: frozenset[str] = field(default_factory=frozenset)

    def to_chunk_metadata(self) -> Metadata:
        """Типизированные структурные поля Section -> chunk.metadata.

        Базовый Section несёт только свой вид; подклассы дописывают
        собственные поля поверх.
        """
        return Metadata.empty().set(SectionKeys.KIND, str(self.SECTION_TYPE))

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

    SECTION_TYPE: ClassVar[SectionKind] = SectionKind.HEADING

    level: int = 1
    text: str = ""

    def to_chunk_metadata(self) -> Metadata:
        return (
            super()
            .to_chunk_metadata()
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

    SECTION_TYPE: ClassVar[SectionKind] = SectionKind.PARAGRAPH


class TableLayout(StrEnum):
    """Раскладка таблицы в чанки.

    GRID — markdown-сетка, шапка повторяется в каждом куске разрезанной
    таблицы. ROWS — строка отдельной записью «колонка: значение»: точечный
    вопрос по справочнику попадает ровно в свою строку, а не в кусок с
    десятком соседних.
    """

    GRID = "grid"
    ROWS = "rows"


class MarkdownToken(StrEnum):
    """Токены markdown-таблицы: сборка строки и экранирование ячейки."""

    PIPE = "|"
    ESCAPED_PIPE = "\\|"
    HEADER_RULE = "---"
    CELL_PAD = " "


class MarkdownTable:
    """Сборка строк markdown-таблицы — одна точка, где живёт эта разметка.

    Зовут её TableSection при рендере GRID-раскладки и парсеры форматов,
    которым нужен тот же вид таблицы вне секции.
    """

    @classmethod
    def row(cls, cells: Sequence[str]) -> str:
        """Строка `| a | b |` с экранированными ячейками."""
        escaped: list[str] = []
        for cell in cells:
            escaped.append(cls.cell(cell))

        glue = f"{MarkdownToken.CELL_PAD}{MarkdownToken.PIPE}{MarkdownToken.CELL_PAD}"
        inner = glue.join(escaped)
        edge = f"{MarkdownToken.PIPE}{MarkdownToken.CELL_PAD}"
        return f"{edge}{inner}{MarkdownToken.CELL_PAD}{MarkdownToken.PIPE}"

    @classmethod
    def rule(cls, width: int) -> str:
        """Разделитель шапки `| --- | --- |` на width колонок."""
        cells: list[str] = []
        for _ in range(width):
            cells.append(str(MarkdownToken.HEADER_RULE))

        return cls.row(cells)

    @staticmethod
    def cell(value: str) -> str:
        """Ячейка в одну строку: переносы схлопнуты, `|` экранирован."""
        flat = " ".join(value.split())
        return flat.replace(MarkdownToken.PIPE, MarkdownToken.ESCAPED_PIPE)


class TableToken(StrEnum):
    """Разметка таблицы в индексируемом тексте: подпись и склейка полей."""

    CAPTION_PREFIX = "Table: "
    FIELD_SEPARATOR = "; "
    VALUE_SEPARATOR = ": "


@dataclass(frozen=True)
class TableSection(Section[str]):
    """Таблица документа: шапка отдельно от строк, чтобы связь колонки со
    значением дожила до чанка.

    Плоский текст таблицу уничтожает — «Параметр Значение connect_timeout
    30s» не отвечает на вопрос, чему равен таймаут. Секция держит колонки и
    строки врозь и отдаёт чанкеру шапку через repeat_header: в каком бы
    месте чанкер ни разрезал длинную таблицу, каждый кусок начинается с
    шапки.

    Строят её парсеры форматов (Confluence, docx, xlsx), потребитель —
    format-aware chunker (StructuralChunker).
    """

    SECTION_TYPE: ClassVar[SectionKind] = SectionKind.TABLE

    GRID_GLUE: ClassVar[str] = "\n"
    """Строки markdown-сетки идут подряд: сетка обязана остаться таблицей."""

    ROWS_GLUE: ClassVar[str] = "\n\n"
    """Записи ROWS разделены пустой строкой: splitter режет по ней и не
    рвёт запись посередине."""

    caption: str = ""
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    layout: TableLayout = TableLayout.GRID

    def to_chunk_metadata(self) -> Metadata:
        meta = super().to_chunk_metadata()
        if self.caption:
            meta = meta.set(SectionKeys.TABLE_CAPTION, self.caption)

        if self.columns:
            meta = meta.set(SectionKeys.TABLE_COLUMNS, self.columns)

        return meta

    def to_format_plan(self) -> FormatPlan:
        if not self.rows:
            return self._caption_only_plan()

        if self.layout is TableLayout.ROWS:
            return self._rows_plan()

        return self._grid_plan()

    def _grid_plan(self) -> FormatPlan:
        lines: list[str] = []
        for row in self.rows:
            lines.append(MarkdownTable.row(row))

        return FormatPlan(
            blocks=self._blocks(lines, self.GRID_GLUE),
            repeat_header=self._grid_header(),
            block_glue=self.GRID_GLUE,
        )

    def _rows_plan(self) -> FormatPlan:
        lines: list[str] = []
        for row in self.rows:
            lines.append(self._named_row(row))

        return FormatPlan(
            blocks=self._blocks(lines, self.ROWS_GLUE),
            repeat_header=self._caption_header(),
            block_glue=self.ROWS_GLUE,
        )

    def _caption_only_plan(self) -> FormatPlan:
        """Таблица без строк: в индекс уходит только подпись, если она есть."""
        header = self._grid_header().strip()
        if not header:
            return FormatPlan()

        return FormatPlan(blocks=self._blocks([header], self.GRID_GLUE))

    def _grid_header(self) -> str:
        parts: list[str] = []
        caption = self._caption_header()
        if caption:
            parts.append(caption)

        if self.columns:
            parts.append(MarkdownTable.row(self.columns) + self.GRID_GLUE)
            parts.append(MarkdownTable.rule(len(self.columns)) + self.GRID_GLUE)

        return "".join(parts)

    def _caption_header(self) -> str:
        if not self.caption:
            return ""

        return f"{TableToken.CAPTION_PREFIX}{self.caption}{self.GRID_GLUE}"

    def _named_row(self, row: tuple[str, ...]) -> str:
        """Строка как «колонка: значение»; лишние колонки идут без имени."""
        fields: list[str] = []
        for index, value in enumerate(row):
            cell = " ".join(value.split())
            if not cell:
                continue

            fields.append(self._named_cell(index, cell))

        return str(TableToken.FIELD_SEPARATOR).join(fields)

    def _named_cell(self, index: int, cell: str) -> str:
        if index >= len(self.columns):
            return cell

        name = self.columns[index]
        if not name:
            return cell

        return f"{name}{TableToken.VALUE_SEPARATOR}{cell}"

    @staticmethod
    def _blocks(lines: Sequence[str], glue: str) -> tuple[FormatBlock, ...]:
        blocks: list[FormatBlock] = []
        cursor = 0
        for line in lines:
            end = cursor + len(line)
            blocks.append(
                FormatBlock(
                    format_content=line,
                    raw_content=line,
                    location=ChunkLocation(start=cursor, end=end),
                    is_atomic=True,
                )
            )
            cursor = end + len(glue)

        return tuple(blocks)


@dataclass(frozen=True)
class OutlineEntry:
    """Строка оглавления документа: уровень заголовка, текст и якорь."""

    level: int
    text: str
    anchor: str = ""


class CardField(StrEnum):
    """Подписи полей карточки документа в индексируемом тексте."""

    TITLE = "Page"
    BREADCRUMB = "Location"
    LABELS = "Labels"
    OUTLINE = "Sections"
    LINKS = "Links"


@dataclass(frozen=True)
class DocumentCardSection(Section[str]):
    """Карточка документа: о чём он, где лежит, из чего состоит, с чем связан.

    Отвечает на вопросы, на которые не отвечает ни один отдельный чанк
    текста: что вообще на этой странице, какое у неё оглавление, какими
    метками помечена, на что ссылается. Собирается парсером формата из
    структуры документа, а не из его пересказа, поэтому выдумать ничего не
    может.

    Идёт первой секцией документа; в metadata помечена SectionKind.CARD,
    чтобы поиск мог отобрать или исключить карточки.
    """

    SECTION_TYPE: ClassVar[SectionKind] = SectionKind.CARD

    LINE_GLUE: ClassVar[str] = "\n"
    ITEM_GLUE: ClassVar[str] = ", "
    OUTLINE_BULLET: ClassVar[str] = "- "
    OUTLINE_INDENT: ClassVar[str] = "  "

    title: str = ""
    breadcrumb: tuple[str, ...] = ()
    outline: tuple[OutlineEntry, ...] = ()
    labels: tuple[str, ...] = ()
    links: tuple[str, ...] = ()

    def to_format_plan(self) -> FormatPlan:
        lines: list[str] = []
        if self.title:
            lines.append(self._field(CardField.TITLE, self.title))

        if self.breadcrumb:
            path = self.ITEM_GLUE.join(self.breadcrumb)
            lines.append(self._field(CardField.BREADCRUMB, path))

        if self.labels:
            marks = self.ITEM_GLUE.join(self.labels)
            lines.append(self._field(CardField.LABELS, marks))

        if self.links:
            lines.append(self._field(CardField.LINKS, self.ITEM_GLUE.join(self.links)))

        if self.outline:
            lines.append(self._outline_block())

        if not lines:
            return FormatPlan()

        body = self.LINE_GLUE.join(lines)
        return FormatPlan(
            blocks=(
                FormatBlock(
                    format_content=body,
                    raw_content=body,
                    location=ChunkLocation(start=0, end=len(body)),
                    is_atomic=True,
                ),
            ),
        )

    def _outline_block(self) -> str:
        base = self._base_level()
        lines: list[str] = [f"{CardField.OUTLINE}:"]
        for entry in self.outline:
            depth = max(0, entry.level - base)
            indent = self.OUTLINE_INDENT * depth
            lines.append(f"{indent}{self.OUTLINE_BULLET}{entry.text}")

        return self.LINE_GLUE.join(lines)

    def _base_level(self) -> int:
        levels: list[int] = []
        for entry in self.outline:
            levels.append(entry.level)

        return min(levels)

    @staticmethod
    def _field(field_name: CardField, value: str) -> str:
        return f"{field_name}: {value}"


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


class SpooledBody(AsyncBinaryStream):
    """Тело, которое транспорт уже сложил в файл на диске.

    Ридер, которому нужен файл (нативный парсер), берёт path и не гоняет байты
    через память; остальные читают как обычный поток. Файл живёт, пока идёт
    fetch транспорта: он его создал, он и удалит.
    """

    CHUNK_SIZE: ClassVar[int] = 1 << 20

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[bytes]:
        with self._path.open("rb") as body:
            while True:
                chunk = await asyncio.to_thread(body.read, self.CHUNK_SIZE)
                if not chunk:
                    return

                yield chunk

    async def read(self) -> bytes:
        return await asyncio.to_thread(self._path.read_bytes)


@dataclass(frozen=True)
class RawDocument:
    """Открытый поток тела + metadata; lifecycle потока — у Transport."""

    handle: AsyncBinaryStream
    """Открытый поток; Reader читает, закрывает его Transport."""

    source_id: SourceId
    """Identity документа; выводит и проставляет Transport (Transport.source_id)."""

    metadata: Metadata = field(default_factory=Metadata.empty)
    """Request.metadata + transport-specific keys; Reader/Chunker мержат свои ключи
    поверх.
    """


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
