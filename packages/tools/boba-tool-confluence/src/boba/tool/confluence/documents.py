"""Reader[str] конвейера индексации на роутере boba-doc: тело вложения из
транспорта уходит в ридер формата через пипу, страницы становятся Section.

Модуль тянет библиотеки форматов, поэтому импортируется лениво — в процессе
приложения его быть не должно.

Ошибки:
IncompatibleContentError — media_type не поддержан или документ не разобран.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import BinaryIO, ClassVar

from boba.doc.bridge import AsyncPipe
from boba.doc.config import DocSection
from boba.doc.document import (
    DocumentError,
    DocumentHint,
    DocumentKind,
    Formats,
    PageWindow,
    ParsedPage,
)
from boba.doc.ocr import OcrEngines
from boba.doc.router import DocumentRouter
from boba.indexing import (
    IncompatibleContentError,
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
    SectionKeys,
    TransportKeys,
)

__all__ = ["DocumentReader"]


class DocumentReader(Reader[str]):
    """Реализация Reader[str] на DocumentRouter: страница документа — Section
    с номером страницы в метаданных; пустые страницы пропускаются."""

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.doc")

    def __init__(self, config: DocSection, engines: OcrEngines) -> None:
        self._router = DocumentRouter(config, engines.of(config.ocr))

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    @property
    def media_types(self) -> tuple[str, ...]:
        """Маршруты DispatchReader'а: документы и картинки, без text/*."""
        return Formats.media_types()

    async def read(self, value: RawDocument) -> AsyncIterator[Section[str]]:
        media_type = value.metadata.get(TransportKeys.CONTENT_TYPE) or ""
        hint = DocumentHint(media_type=media_type)
        kind = Formats.of_hint(hint)
        if kind is DocumentKind.UNKNOWN:
            raise self._incompatible(
                value, f"media type {media_type!r} is not a readable document"
            )

        try:
            pages = await AsyncPipe.run(value.handle, self._consumer(kind))
        except DocumentError as exc:
            raise self._incompatible(value, str(exc)) from exc

        for section in self._sections(value, pages, kind):
            yield section

    def _consumer(
        self, kind: DocumentKind
    ) -> Callable[[BinaryIO], Sequence[ParsedPage]]:
        def consume(source: BinaryIO) -> Sequence[ParsedPage]:
            with self._router.open_as(kind, source) as document:
                return tuple(document.pages(PageWindow.whole()))

        return consume

    @staticmethod
    def _sections(
        value: RawDocument, pages: Sequence[ParsedPage], kind: DocumentKind
    ) -> Iterator[Section[str]]:
        for page in pages:
            text = page.text.strip()
            if not text:
                continue

            metadata = value.metadata.set(ReaderKeys.DOC_TYPE, kind.value)
            metadata = metadata.set(SectionKeys.PAGE_NUMBER, page.number)
            yield Section(
                source_id=value.source_id,
                content=text,
                order=page.number,
                metadata=metadata,
            )

    def _incompatible(
        self, value: RawDocument, reason: str
    ) -> IncompatibleContentError:
        return IncompatibleContentError(
            reader_id=str(self.READER_ID),
            canonical_id=str(value.source_id),
            reason=reason,
        )
