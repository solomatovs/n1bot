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
    """Парсер RawDocument → Section[T]'ы для одной модальности content'а."""

    @abstractmethod
    def reader_id(self) -> ReaderId: ...

    @abstractmethod
    def convert(self, value: RawDocument) -> Iterable[Section[T]]: ...


class PlainTextReader(Reader[str]):
    """
    Reader[str] для плоского текста: читает handle целиком и эмитит один
    Section[str] с полным content. Метит metadata как `text/plain`.
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
            anchor=None,
            order=0,
            metadata=value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE),
        )
