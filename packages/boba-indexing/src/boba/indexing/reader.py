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
from typing import TypeVar

from boba.indexing.raw_document import RawDocument
from boba.indexing.sections import Section
from boba.patterns import Converter, StateFull, StrId

__all__ = ["Reader", "ReaderId"]

T = TypeVar("T")


class ReaderId(StrId):
    """
    Идентификатор Reader-реализации (например 'ext.text', 'ext.confluence_html')
    """


class Reader(
    Converter[RawDocument, Iterable[Section[T]]],
    StateFull,
):
    """Парсер RawDocument → Section[T]'ы для одной модальности content'а."""

    @abstractmethod
    def reader_id(self) -> ReaderId: ...

    @abstractmethod
    def convert(self, value: RawDocument) -> Iterable[Section[T]]: ...
