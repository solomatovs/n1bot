"""Общая механика «документ по страницам -> Section[str]».

Всё, что не зависит от способа парсинга: карта форматов (DocumentMedia),
сборка секций (PageSectionBuilder) и база ридера (PagedDocumentReader).
Конкретные ридеры добавляют только parse_pages: LiteParseReader зовёт
движок прямо, SandboxLiteParseReader — payload в песочнице. Формат
выводится из TransportKeys.CONTENT_TYPE, по которому DispatchReader и так
роутит, — ридеры остаются generic, без знания об источнике.

Ошибки: IncompatibleContentError — неподдерживаемый content_type или битый
документ (PagedDocumentReader.read, чтобы pipeline изолировал документ как
SourceFailed); ValueError — из DocumentMedia.suffix_for вне ридера.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import ClassVar

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
from boba.liteparse import ParsedPage

__all__ = ["DocumentMedia", "PagedDocumentReader", "PageSectionBuilder"]


class DocumentMedia:
    """media_type <-> расширение <-> doc_type: одна карта форматов на всех."""

    SUFFIX_BY_MEDIA_TYPE: ClassVar[Mapping[str, str]] = {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation": ".pptx",
    }
    """media_type -> расширение. Ключи задают и набор поддерживаемых типов
    (media_types для routes DispatchReader'а), и суффикс для liteparse."""

    @classmethod
    def media_types(cls) -> tuple[str, ...]:
        """Поддерживаемые media_type — для сборки routes DispatchReader'а."""
        return tuple(cls.SUFFIX_BY_MEDIA_TYPE)

    @staticmethod
    def normalize(content_type: str) -> str:
        """media_type без параметров (`; charset=...`), lower-case."""
        base = content_type.split(";", 1)[0]
        return base.strip().lower()

    @classmethod
    def suffix_for(cls, content_type: str | None) -> str:
        """Расширение для media_type; неподдерживаемый или пустой -> ValueError."""
        if not content_type:
            raise cls._unsupported(content_type)

        normalized = cls.normalize(content_type)
        if normalized not in cls.SUFFIX_BY_MEDIA_TYPE:
            raise cls._unsupported(content_type)

        return cls.SUFFIX_BY_MEDIA_TYPE[normalized]

    @staticmethod
    def doc_type_of(suffix: str) -> str:
        """'.pdf' -> 'pdf' — значение ReaderKeys.DOC_TYPE."""
        return suffix.lstrip(".")

    @staticmethod
    def filename_for(suffix: str) -> str:
        """Синтетическое имя файла: liteparse узнаёт формат по расширению."""
        return f"document{suffix}"

    @staticmethod
    def _unsupported(content_type: str | None) -> ValueError:
        return ValueError(f"unsupported content_type={content_type!r}")


class PageSectionBuilder:
    """Страницы документа -> Section[str] с локусом цитирования."""

    @staticmethod
    def build(
        value: RawDocument,
        pages: Sequence[ParsedPage],
        doc_type: str,
    ) -> Iterator[Section[str]]:
        """Пустые страницы пропускаются; к метадате источника добавляются
        только структурные ReaderKeys.DOC_TYPE и SectionKeys.PAGE_NUMBER."""
        for page in pages:
            text = page.text.strip()
            if not text:
                continue

            metadata = value.metadata.set(ReaderKeys.DOC_TYPE, doc_type)
            metadata = metadata.set(SectionKeys.PAGE_NUMBER, page.page_num)
            yield Section(
                source_id=value.source_id,
                content=text,
                order=page.page_num,
                metadata=metadata,
            )


class PagedDocumentReader(Reader[str]):
    """База ридеров liteparse: бинарь документа -> Section на страницу."""

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.liteparse")

    PARSE_ERRORS: ClassVar[tuple[type[Exception], ...]] = ()
    """Исключения parse_pages, означающие «битый документ»; задаёт наследник."""

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    @property
    def media_types(self) -> tuple[str, ...]:
        """Поддерживаемые media_type — для сборки routes DispatchReader'а."""
        return DocumentMedia.media_types()

    def read(self, value: RawDocument) -> Iterable[Section[str]]:
        suffix = self._resolve_suffix(value)

        data = value.handle.read()
        if not data:
            return []

        try:
            pages = self.parse_pages(data, DocumentMedia.filename_for(suffix))
        except self.PARSE_ERRORS as e:
            raise self._incompatible(value, str(e)) from e

        doc_type = DocumentMedia.doc_type_of(suffix)
        return PageSectionBuilder.build(value, pages, doc_type)

    @abstractmethod
    def parse_pages(self, data: bytes, filename: str) -> Sequence[ParsedPage]:
        """Распарсить байты документа в страницы; формат — по расширению filename."""

    def _resolve_suffix(self, value: RawDocument) -> str:
        content_type = value.metadata.get(TransportKeys.CONTENT_TYPE)
        try:
            return DocumentMedia.suffix_for(content_type)
        except ValueError as e:
            raise self._incompatible(value, str(e)) from e

    def _incompatible(
        self, value: RawDocument, reason: str
    ) -> IncompatibleContentError:
        return IncompatibleContentError(
            reader_id=str(self.reader_id()),
            canonical_id=str(value.source_id),
            reason=reason,
        )
