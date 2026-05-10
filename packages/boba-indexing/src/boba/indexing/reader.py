"""
Reader[T] - порт для получения логических фрагментов документа (Section[T])
из сырого документа (RawDocument)

Generic над типом content:
    TextReader → Reader[str] для текстовых документов
    ImageReader → Reader[bytes] для изображений

Явная реализация Generic типа T определяется в pipeline

Reader открывает handle, парсит, yield'ит Section[T].
Ставит `Section.source_id`, пробрасывает identity_hints в Section.metadata.

Reader явно указан в pipeline'е — без autodetect и dispatcher'а.
Если ему пришёл несовместимый payload — `IncompatibleContentError`
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from typing import ClassVar, TypeVar

from boba.indexing.errors import IncompatibleContentError
from boba.indexing.metadata import ReaderKeys
from boba.indexing.raw_document import RawDocument
from boba.indexing.sections import Section
from boba.patterns import Converter, StateFull, StrId

__all__ = ["PlainTextReader", "Reader", "ReaderId"]

T = TypeVar("T")


class ReaderId(StrId):
    """
    Идентификатор Reader-реализации (например 'ext.text', 'ext.confluence_html')
    """


class Reader(
    Converter[RawDocument, Iterable[Section[T]]],
    StateFull,
):
    """
    Разбирает `RawDocument` на логические разделы документа (`Section[T]`)
    `RawDocument` - обычно это открытый файловый дескриптор на сырые данные
    `Section[T]` - это логическая секция внутри сырого потока данных

    **Схема**:
    ```python
    RawDocument  ───────────────────────────────reader.convert──→  Iterable[Section[T]]
        handle      : BinaryStream                                  │
        source_id   : SourceId  ──pass──────────────────────────→  source_id   (тот же)
        metadata    : Metadata  ──merge─────────────────────────→  metadata    (+ дополняет своей meta: ReaderKeys.DOC_TYPE …)
                                                                →    content     : T          (распарсенный фрагмент)
                                                                →    anchor      : str|None   (heading-id, page …; None у плоских)
                                                                →    order       : int        (порядок в документе, для детерминизма chunk_id)
    ```

    **Контракты**:
    - читает `handle` (целиком или потоково), закрытие — обязанность Transport
    - на несовместимый payload бросает `IncompatibleContentError`
    - явно указывается в pipeline, никакого autodetect / dispatch

    **Пример** (usage `MarkdownReader`):
    ```python
    reader: Reader[str] = MarkdownReader()

    raw = RawDocument(
        handle=BytesIO(
            b"preamble before any heading\\n\\n"
            b"# Intro\\nintro body\\n\\n"
            b"## API\\napi body"
        ),
        source_id=SourceId("doc1"),
    )

    # 1 RawDocument → 3 Section'и: preamble + 2 heading'а
    list(reader.convert(raw)) == [
        Section(
            source_id=SourceId("doc1"),                  # pass из RawDocument
            content="preamble before any heading",       # новое: фрагмент исходного текста
            anchor=None,                                 # новое: None у preamble (нет heading'а)
            order=0,                                     # новое: позиция в документе
            metadata=Metadata.empty().set(ReaderKeys.DOC_TYPE, "markdown"),  # merge: + DOC_TYPE
        ),
        Section(
            source_id=SourceId("doc1"),
            content="# Intro\\n\\nintro body",
            anchor="intro",                              # новое: slug текста heading'а
            order=1,
            metadata=(
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "markdown")
                .set(MarkdownKeys.HEADING_LEVEL, 1)      # merge: + структурные ключи
                .set(MarkdownKeys.HEADING_TEXT, "Intro")
            ),
        ),
        Section(
            source_id=SourceId("doc1"),
            content="## API\\n\\napi body",
            anchor="api",
            order=2,
            metadata=(
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "markdown")
                .set(MarkdownKeys.HEADING_LEVEL, 2)
                .set(MarkdownKeys.HEADING_TEXT, "API")
            ),
        ),
    ]
    ```
    """  # noqa: E501

    @abstractmethod
    def reader_id(self) -> ReaderId: ...

    @abstractmethod
    def convert(self, value: RawDocument) -> Iterable[Section[T]]: ...


class PlainTextReader(Reader[str]):
    """
    Простейший `Reader[str]`: декодирует handle целиком и эмитит один Section.

    **Схема**:
    ```python
    RawDocument(handle=b"hello world", source_id="doc1")
        │
        └─ handle.read() → bytes ──decode(encoding)──→ str
                                                        │
                                                        ▼
    Section(source_id="doc1", content="hello world",
            anchor=None, order=0,
            metadata={ReaderKeys.DOC_TYPE: "text/plain"})
    ```

    Бросает `IncompatibleContentError` если bytes не декодируются указанным
    `encoding`. Anchor всегда `None` — секция плоская, без heading'ов.

    **Пример**:
    ```python
    reader = PlainTextReader(encoding="utf-8")
    raw = RawDocument(handle=BytesIO(b"line1\\nline2"), source_id=SourceId("file.txt"))

    list(reader.convert(raw)) == [
        Section(SourceId("file.txt"), "line1\\nline2", anchor=None, order=0,
                metadata=Metadata({ReaderKeys.DOC_TYPE: "text/plain"})),
    ]
    ```
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.text")
    DEFAULT_ENCODING: ClassVar[str] = "utf-8"
    DOC_TYPE: ClassVar[str] = "text/plain"

    def __init__(self, encoding: str = DEFAULT_ENCODING) -> None:
        self._encoding = encoding

    def name(self) -> str:
        return "PlainTextReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        raw = value.handle.read()
        try:
            text = raw.decode(self._encoding)
        except UnicodeDecodeError as e:
            raise IncompatibleContentError(
                reader_id=self.READER_ID.to_wire(),
                canonical_id=value.source_id.to_wire(),
                reason=f"cannot decode with {self._encoding!r}: {e}",
            ) from e

        yield Section(
            source_id=value.source_id,
            content=text,
            order=0,
            metadata=value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE),
        )
