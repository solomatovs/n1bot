"""
Section[T] — логический фрагмент документа.

Доменная иерархия — намеренно тонкая. В домене живёт только то, что
эмитится **каждым** Reader'ом и имеет cross-format-смысл:

- `Section[T]`        — базовый dataclass.
- `HeadingSection`    — заголовочная секция (`level` + `text`).
                         Универсальна: markdown `# ...`, html `<hN>`, и т.п.
- `ParagraphSection`  — обычный текстовый блок и universal fallback для всего,
                         что Reader не разобрал в более конкретный тип.

Format-specific типы (markdown-таблицы, markdown-списки, code-fence'ы,
блок-цитаты, hr) живут в format-package (`boba-markdown`, ...) как
наследники `Section`. Открытая иерархия — расширяй где угодно через
наследование `Section[T]` и переопределение `to_chunk_metadata()`.

Контракт:

- Координаты/идентификаторы (location, anchor) — в `metadata` через
  `SectionKeys.LOCATION_START` / `LOCATION_END` / `ANCHOR`. Не у каждого
  формата они есть (HTML без lxml-sourceline-tracking не даёт offset'ов),
  поэтому `Section` их не required-полями: парсер пишет в metadata то,
  что умеет, consumer читает с обработкой `None`.
- Структурные типизированные поля (`level`, `text`, ...) живут как
  атрибуты подклассов и переезжают в `chunk.metadata` через
  `to_chunk_metadata()`.
- `SECTION_TYPE` — короткий canonical alias, удобный для логов.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Generic, TypeVar

from boba.indexing.metadata import Metadata, MetadataKey
from boba.patterns import StrId

__all__ = [
    "HeadingSection",
    "ParagraphSection",
    "Section",
    "SectionKeys",
    "SourceId",
]

T = TypeVar("T")


class SourceId(StrId):
    """Стабильный canonical id документа-источника (URL, fs-path, doc-key)."""


class SectionKeys:
    """Стандартные `MetadataKey`-и для атрибутов доменных Section'ов.

    `LOCATION_START` / `LOCATION_END` / `ANCHOR` — координаты/идентификаторы;
    парсеры пишут их в `section.metadata` если умеют, для других форматов
    отсутствуют.

    `HEADING_LEVEL` / `HEADING_TEXT` — структурные поля `HeadingSection`,
    эмитятся через `to_chunk_metadata()`.

    Format-specific подклассы (в `boba-markdown` и т.п.) определяют свои
    собственные `*Keys`-классы рядом со своими Section-типами.
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


@dataclass(frozen=True)
class Section(Generic[T]):
    """Логический фрагмент документа. Базовый класс открытой иерархии.

    Подклассы добавляют типизированные структурные поля и переопределяют
    `to_chunk_metadata()` для эмиссии этих полей в `chunk.metadata`.

    Поля:

    - `source_id` — id source-документа.
    - `content`   — текст/bytes раздела (для текстовых форматов — `T = str`).
    - `order`     — порядок секции в исходном документе (детерминирует chunk_id).
    - `metadata`  — пробрасываемая metadata. Сюда же парсер кладёт
                     координаты/идентификаторы через `SectionKeys.LOCATION_*`
                     и `SectionKeys.ANCHOR`, если умеет их вычислить.
    - `tags`      — множество тэгов.
    """

    SECTION_TYPE: ClassVar[str] = "section"

    source_id: SourceId
    content: T
    order: int = 0
    metadata: Metadata = field(default_factory=Metadata.empty)
    tags: frozenset[str] = field(default_factory=frozenset)

    def to_chunk_metadata(self) -> Metadata:
        """Типизированные структурные поля Section → `chunk.metadata`.

        Базовый Section не несёт типизированных полей — возвращает
        `Metadata.empty()`. Подклассы переопределяют.
        """
        return Metadata.empty()


@dataclass(frozen=True)
class HeadingSection(Section[str]):
    """Раздел с heading-маркером (`# `, `<h1>`, ...).

    `content` — оригинальный текст раздела с разметкой формата;
    `level` и `text` — разобранная типизированная информация.
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


@dataclass(frozen=True)
class ParagraphSection(Section[str]):
    """Обычный текстовый параграф / fallback для не-типизированного контента.

    Inline-разметка остаётся в `content` (не разворачивается в подсекции).
    """

    SECTION_TYPE: ClassVar[str] = "paragraph"
