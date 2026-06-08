"""LiteParseReader — Reader[str] поверх LiteParseEngine.

Бинарь документа (PDF/docx/xlsx/pptx) -> одна Section на страницу/лист.
Формат liteparse определяет по расширению, поэтому суффикс выводится из
TransportKeys.CONTENT_TYPE (по которому DispatchReader и так роутит) —
ридер остаётся generic, без знания о Confluence/источнике.

Метаданные источника (source_id, url, page_id, ...) ридер пробрасывает
из RawDocument.metadata как есть; сам добавляет только структурные
ReaderKeys.DOC_TYPE и SectionKeys.PAGE_NUMBER (локус цитирования).

Ошибки парсера заворачиваются в IncompatibleContentError, чтобы pipeline
изолировал битый документ как SourceFailed, а не падал весь прогон.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
from boba.liteparse.engine import LiteParseEngine
from boba.liteparse.errors import LiteParseError
from boba.liteparse.params import LiteParseParams

__all__ = ["LiteParseReader"]


class LiteParseReader(Reader[str]):
    """Reader[str]: документ liteparse -> Section[str] на страницу."""

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.liteparse")

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

    def __init__(
        self,
        params: LiteParseParams | None = None,
        *,
        suffixes: Mapping[str, str] | None = None,
        reader_id: ReaderId = READER_ID,
    ) -> None:
        self._params = params or LiteParseParams()
        self._suffixes = dict(
            suffixes if suffixes is not None else self.SUFFIX_BY_MEDIA_TYPE
        )
        self._reader_id = reader_id

    def reader_id(self) -> ReaderId:
        return self._reader_id

    @property
    def media_types(self) -> tuple[str, ...]:
        """Поддерживаемые media_type — для сборки routes DispatchReader'а."""
        return tuple(self._suffixes)

    def read(self, value: RawDocument) -> Iterable[Section[str]]:
        content_type = value.metadata.get(TransportKeys.CONTENT_TYPE)
        suffix = self._suffix_for(content_type)
        if suffix is None:
            raise IncompatibleContentError(
                reader_id=str(self._reader_id),
                canonical_id=str(value.source_id),
                reason=f"unsupported content_type={content_type!r}",
            )
        data = value.handle.read()
        if not data:
            return
        try:
            result = LiteParseEngine.parse(self._params, data, f"document{suffix}")
        except LiteParseError as e:
            raise IncompatibleContentError(
                reader_id=str(self._reader_id),
                canonical_id=str(value.source_id),
                reason=str(e),
            ) from e

        doc_type = suffix.lstrip(".")
        for page in result.pages:
            text = page.text.strip()
            if not text:
                continue
            yield Section(
                source_id=value.source_id,
                content=text,
                order=page.page_num,
                metadata=(
                    value.metadata.set(ReaderKeys.DOC_TYPE, doc_type).set(
                        SectionKeys.PAGE_NUMBER, page.page_num
                    )
                ),
            )

    def _suffix_for(self, content_type: str | None) -> str | None:
        """media_type (без параметров, lower-case) -> расширение; None если не наш."""
        if not content_type:
            return None
        base = content_type.split(";", 1)[0].strip().lower()
        return self._suffixes.get(base)
