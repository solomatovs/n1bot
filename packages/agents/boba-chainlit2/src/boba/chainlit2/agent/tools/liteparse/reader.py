"""Reader[str] для indexing-pipeline: документ парсится в песочнице.

Замена boba.liteparse.LiteParseReader: тот же контракт (Section на страницу,
битый документ -> IncompatibleContentError, чтобы pipeline изолировал его как
SourceFailed), но liteparse исполняется не в процессе приложения.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import ClassVar

from boba.chainlit2.agent.tools.liteparse.caller import LiteParseCaller
from boba.chainlit2.sandbox import SandboxPayloadError
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

__all__ = ["SandboxLiteParseReader"]


class SandboxLiteParseReader(Reader[str]):
    """Документ вложения -> Section[str] на страницу; парсит песочница."""

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
        caller: LiteParseCaller,
        *,
        suffixes: Mapping[str, str] | None = None,
        reader_id: ReaderId = READER_ID,
    ) -> None:
        self._caller = caller
        if suffixes is None:
            suffixes = self.SUFFIX_BY_MEDIA_TYPE
        self._suffixes = dict(suffixes)
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
            answer = self._caller.parse_bytes(data, f"document{suffix}")
        except SandboxPayloadError as e:
            raise IncompatibleContentError(
                reader_id=str(self._reader_id),
                canonical_id=str(value.source_id),
                reason=str(e),
            ) from e

        doc_type = suffix.lstrip(".")
        for page in answer.pages:
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
