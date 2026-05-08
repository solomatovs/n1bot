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

from boba.patterns import Converter, StateFull, StrId
from boba.processing.raw_document import RawDocument
from boba.processing.sections import Section

__all__ = ["Reader", "ReaderId"]


class ReaderId(StrId):
    """Идентификатор Reader-реализации (например 'ext.text', 'ext.confluence_html')."""


class Reader(
    Converter[RawDocument, Iterable[Section]],
    StateFull,
    ABC,
):
    """
    Парсер RawDocument → Section'ы.
    """

    @abstractmethod
    def reader_id(self) -> ReaderId: ...

    @abstractmethod
    def convert(self, value: RawDocument) -> Iterable[Section]: ...
