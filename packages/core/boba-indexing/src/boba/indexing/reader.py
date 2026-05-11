"""Reader[T] — порт для получения логических фрагментов документа.

Generic над типом content:
    TextReader  → Reader[str]   для текстовых документов
    ImageReader → Reader[bytes] для изображений

Reader открывает handle, парсит, yield'ит `Section[T]`. Ставит
`Section.source_id`, пробрасывает upstream-metadata в `Section.metadata`.

Reader явно указан в pipeline'е — без autodetect и dispatcher'а.
Если ему пришёл несовместимый payload — `IncompatibleContentError`.

В домене живёт только interface. Конкретные impl'ы — в format/feature-
package'ах (`boba-text.PlainTextReader`, `boba-markdown.MarkdownReader`,
`boba-html.HtmlHeadingReader`, …).
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from typing import TypeVar

from boba.indexing.raw_document import RawDocument
from boba.indexing.sections import Section
from boba.patterns import Converter, StateFull, StrId

__all__ = ["Reader", "ReaderId"]

T = TypeVar("T")


class ReaderId(StrId):
    """Идентификатор Reader-реализации (например `ext.text`, `ext.markdown`)."""


class Reader(
    Converter[RawDocument, Iterable[Section[T]]],
    StateFull,
):
    """Разбирает `RawDocument` на логические разделы (`Section[T]`).

    **Схема**:
    ```python
    RawDocument  ──reader.convert──→  Iterable[Section[T]]
        handle    : BinaryStream
        source_id : SourceId   ──pass──→  source_id
        metadata  : Metadata   ──merge─→  metadata  (+ ReaderKeys.DOC_TYPE …)
                                       →  content : T  (распарсенный фрагмент)
                                       →  order   : int (для детерминизма chunk_id)
    ```

    **Контракты**:
    - читает `handle` (целиком или потоково), закрытие — обязанность Transport.
    - на несовместимый payload бросает `IncompatibleContentError`.
    - явно указывается в pipeline, никакого autodetect / dispatch.
    """

    @abstractmethod
    def reader_id(self) -> ReaderId: ...

    @abstractmethod
    def convert(self, value: RawDocument) -> Iterable[Section[T]]: ...
