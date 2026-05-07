"""Reader: RawDocument (handle) → Iterable[Section].

Reader открывает handle, парсит, yield'ит Section'ы. Ставит
`Section.source_id = doc.canonical_id`, пробрасывает identity_hints
в Section.metadata.

Reader явно указан в pipeline'е — без autodetect и dispatcher'а. Если
ему пришёл несовместимый payload — это **ошибка сборки pipeline**,
Reader волен бросать `IncompatibleContentError` или своё domain-исключение.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from boba.patterns import ContextConverter, StateFull, StrId
from boba.processing.context import IndexingContext
from boba.processing.raw_document import RawDocument
from boba.processing.sections import Section

__all__ = ["Reader", "ReaderId"]


class ReaderId(StrId):
    """Идентификатор Reader-реализации (например 'ext.text', 'ext.confluence_html')."""


class Reader(
    ContextConverter[IndexingContext, RawDocument, Iterable[Section]],
    StateFull,
    ABC,
):
    """Парсер RawDocument → Section'ы.

    Контракт:
    - Внутри `convert()` открывает `value.open()`, читает, парсит, **закрывает**
      handle (через `with`).
    - `Section.source_id = value.canonical_id` (если пуст — fallback на
      то, что положил RequestSource в `Request.url`).
    - `Section.metadata` обогащается через `value.identity_hints`.
    """

    @abstractmethod
    def reader_id(self) -> ReaderId: ...

    @abstractmethod
    def convert(
        self, ctx: IndexingContext, value: RawDocument
    ) -> Iterable[Section]: ...
