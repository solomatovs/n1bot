"""Reader[T] — порт парсинга RawDocument в логические фрагменты Section[T]."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Generic, NewType, TypeVar

from boba.indexing.raw_document import RawDocument
from boba.indexing.sections import Section

__all__ = ["Reader", "ReaderId"]

T = TypeVar("T")


ReaderId = NewType("ReaderId", str)
"""Идентификатор Reader-реализации (например ext.text, ext.markdown)."""


class Reader(ABC, Generic[T]):
    """Разбирает RawDocument на Section[T].

    Handle не закрывает (это Transport), на несовместимый payload бросает IncompatibleContentError, autodetect'а нет.
    """

    @abstractmethod
    def reader_id(self) -> ReaderId: ...

    @abstractmethod
    def read(self, raw: RawDocument) -> Iterable[Section[T]]: ...
