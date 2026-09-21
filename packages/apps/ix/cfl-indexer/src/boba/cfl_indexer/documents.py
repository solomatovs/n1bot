"""Текст вложения для индекса: решение, брать ли файл, и извлечение текста.

Гейт из boba-confluence решает по маскам конфига и флагу OCR, качать ли вложение;
существование вложения он не отменяет: node и surface-строка пишутся всегда, текст
только у взятых. Текстовые типы декодируются перебором кодировок, документы и
картинки идут в liteparse (OCR по флагу); текст картинки — аспект ocr, остальное —
body.

Ошибки:
DocumentTextError — файл не разобран: liteparse отказал, кодировка не подошла.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from boba.cfl_indexer.confluence import AttachmentSummary
from boba.cfl_indexer.store import Aspect, PushedText
from boba.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    AttachmentInfo,
    AttachmentVerdict,
)
from boba.liteparse.engine import LiteParseEngine
from boba.text.document import DocumentMedia, LiteParseError, LiteParseParams
from boba.text.reader import TextMedia

__all__ = ["AttachmentText", "DocumentTextError", "TextParams"]


class DocumentTextError(Exception):
    """Текст из файла вложения не извлечён."""


class TextParams(BaseModel):
    """Что решает и как разбирает вложения: маски, OCR, кодировки текстовых файлов."""

    model_config = ConfigDict(frozen=True)

    masks: Sequence[str]
    encodings: Sequence[str]
    liteparse: LiteParseParams


class AttachmentText:
    """Вердикт по вложению и текст его файла."""

    PAGE_SEPARATOR: ClassVar[str] = "\n\n"
    IMAGE_PREFIX: ClassVar[str] = "image/"

    def __init__(self, params: TextParams) -> None:
        self._params = params
        self._gate = AttachmentGate(
            allowed=AttachmentFilter.of_masks(params.masks),
            requested=True,
            ocr=params.liteparse.ocr_enabled,
        )

    def verdict(self, attachment: AttachmentSummary) -> AttachmentVerdict:
        info = AttachmentInfo(
            id=attachment.id,
            title=attachment.title,
            media_type=attachment.media_type,
            file_size=attachment.file_size,
            download_path=attachment.download_path,
            webui="",
            version=attachment.version,
            when="",
        )
        verdict = self._gate.verdict(info)
        if verdict is not AttachmentVerdict.TAKE:
            return verdict

        if not self.supported(attachment.media_type):
            return AttachmentVerdict.NOT_ALLOWED

        return AttachmentVerdict.TAKE

    @staticmethod
    def supported(media_type: str) -> bool:
        normalized = DocumentMedia.normalize(media_type)
        if normalized in TextMedia.DOC_TYPE_BY_MEDIA_TYPE:
            return True

        return normalized in DocumentMedia.SUFFIX_BY_MEDIA_TYPE

    async def extract(
        self, attachment: AttachmentSummary, path: Path
    ) -> Sequence[PushedText]:
        """Текст файла как аспекты индекса; пустой текст не даёт ни одного."""
        normalized = DocumentMedia.normalize(attachment.media_type)
        if normalized in TextMedia.DOC_TYPE_BY_MEDIA_TYPE:
            content = self._decode(path, attachment)
        else:
            content = await asyncio.to_thread(self._parse, path, attachment)

        if not content:
            return ()

        aspect = Aspect.BODY
        if normalized.startswith(self.IMAGE_PREFIX):
            aspect = Aspect.OCR

        return (PushedText(aspect=aspect, content=content),)

    def _decode(self, path: Path, attachment: AttachmentSummary) -> str:
        raw = path.read_bytes()
        for encoding in self._params.encodings:
            try:
                return raw.decode(encoding).strip()
            except UnicodeDecodeError:
                continue

        tried = ", ".join(self._params.encodings)
        raise DocumentTextError(
            f"attachment {attachment.id} {attachment.title!r}: cannot decode "
            f"{len(raw)} bytes with any of: {tried}"
        )

    def _parse(self, path: Path, attachment: AttachmentSummary) -> str:
        suffix = DocumentMedia.suffix_for(attachment.media_type)
        filename = DocumentMedia.filename_for(suffix)
        try:
            result = LiteParseEngine.parse_file_as(
                self._params.liteparse, path, filename
            )
        except LiteParseError as exc:
            raise DocumentTextError(
                f"attachment {attachment.id} {attachment.title!r} "
                f"({attachment.media_type}): {exc}"
            ) from exc

        pages: list[str] = []
        for page in result.pages:
            text = str(page.text).strip()
            if not text:
                continue

            pages.append(text)

        return self.PAGE_SEPARATOR.join(pages)
