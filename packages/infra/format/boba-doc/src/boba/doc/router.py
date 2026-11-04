"""Точка входа чтения документов: вид по подсказке транспорта и первым
байтам, выбор ридера формата, время жизни открытого документа и текст
документа целиком.

Ошибки:
DocumentError — вид не определён или не поддержан, документ не открыт
    или страница не прочитана.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import ClassVar

from boba.doc.document import (
    ByteStream,
    Document,
    DocumentError,
    DocumentHint,
    DocumentKind,
    Formats,
    FormatSpec,
    PageWindow,
    ParsedPage,
    Prefixed,
)

__all__ = ["DocumentRouter", "ReaderSpec"]


@dataclass(frozen=True)
class ReaderSpec:
    """Формат и уже настроенная фабрика документа, передаваемые в роутер."""

    format: FormatSpec
    open: Callable[[ByteStream], Document]


class DocumentRouter:
    """Открывает документ по потоку байтов: читает голову, определяет вид и
    отдаёт его ридеру формата; текст документа целиком склеивает из страниц.
    Один экземпляр на процесс, OCR-движок общий. Его создают тела инструментов
    doc/web/confluence и индексатор вложений."""

    PAGE_GLUE: ClassVar[str] = "\n\n"

    def __init__(self, readers: Sequence[ReaderSpec]) -> None:
        formats = (reader.format for reader in readers)
        self.formats = Formats(formats)
        self._readers = {reader.format.kind: reader.open for reader in readers}

    def detect(
        self, stream: ByteStream, hint: DocumentHint
    ) -> tuple[DocumentKind, ByteStream]:
        """Вид документа по подсказке и голове потока; голова возвращается
        в поток, который дальше читает ридер."""
        head = stream.read(Formats.HEAD_SIZE)
        kind = self.formats.detect(hint, head)
        if kind is None:
            raise DocumentError(
                f"document {hint.filename!r} ({hint.media_type!r}): format not "
                f"recognized by media type, filename or leading bytes"
            )

        return kind, Prefixed(head, stream)

    @contextmanager
    def open(
        self, stream: ByteStream, hint: DocumentHint
    ) -> Generator[Document, None, None]:
        kind, prefixed = self.detect(stream, hint)

        with self.open_as(kind, prefixed) as document:
            yield document

    def read_text(self, stream: ByteStream, hint: DocumentHint) -> str:
        """Текст всех страниц одним куском: пустые страницы опускаются."""
        kind, prefixed = self.detect(stream, hint)

        return self.read_text_as(kind, prefixed)

    def read_text_as(self, kind: DocumentKind, stream: ByteStream) -> str:
        with self.open_as(kind, stream) as document:
            return self._join(document.pages(PageWindow.whole()))

    def _join(self, pages: Iterable[ParsedPage]) -> str:
        return self.PAGE_GLUE.join(self._texts(pages))

    def _texts(self, pages: Iterable[ParsedPage]) -> Iterator[str]:
        for page in pages:
            text = page.text.strip()
            if not text:
                continue

            yield text

    @contextmanager
    def open_as(
        self, kind: DocumentKind, stream: ByteStream
    ) -> Generator[Document, None, None]:
        document = self._open(kind, stream)
        try:
            yield document
        finally:
            try:
                document.close()
            except DocumentError:
                raise
            except Exception as exc:
                raise DocumentError(
                    f"closing document format {kind!r} failed: {exc}"
                ) from exc

    def _open(self, kind: DocumentKind, stream: ByteStream) -> Document:
        factory = self._readers.get(kind)
        if factory is None:
            raise DocumentError(f"document format {kind!r} has no registered reader")

        try:
            return factory(stream)
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError(
                f"opening document format {kind!r} failed: {exc}"
            ) from exc
