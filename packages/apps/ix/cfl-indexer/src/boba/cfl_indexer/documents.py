"""Текст вложений: вердикт по маскам и OCR, вид документа по media-type и имени,
извлечение текста роутером boba-doc прямо из потока скачивания.

Ошибки:
DocumentTextError — файл не распознан роутером или не разобран.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import BinaryIO, ClassVar

from boba.cfl_indexer.confluence import Attachment
from boba.cfl_indexer.store import Aspect
from boba.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    AttachmentInfo,
    AttachmentVerdict,
)
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

__all__ = ["AttachmentReader", "DocumentTextError"]


class DocumentTextError(Exception):
    """Текст из файла вложения не извлечён."""


class AttachmentReader:
    """Вложения одного обхода: что брать и как читать.

    Создаётся обходом спейса из секции чтения документов и масок вложений:
    внутри собираются роутер boba-doc с движком OCR и гейт администратора.
    Решает вердикт по вложению, аспект его текста и даёт потребителя потока
    скачивания, который читает файл роутером и склеивает страницы.
    """

    PAGE_SEPARATOR: ClassVar[str] = "\n\n"

    def __init__(self, doc: DocSection, masks: Sequence[str]) -> None:
        self._router = DocumentRouter(doc, OcrEngines.of(doc.ocr))
        self._gate = AttachmentGate(
            allowed=AttachmentFilter(masks), requested=True, ocr=doc.ocr.enabled
        )

    def decide(self, attachment: Attachment) -> AttachmentVerdict:
        info = AttachmentInfo(
            id=attachment.id,
            title=attachment.title,
            media_type=attachment.media_type,
            file_size=attachment.file_size,
            download_path=attachment.download_path,
            version=attachment.version,
        )
        verdict = self._gate.verdict(info)
        if verdict is not AttachmentVerdict.TAKE:
            return verdict

        kind = self.kind(attachment)
        if kind is DocumentKind.UNKNOWN:
            return AttachmentVerdict.NOT_ALLOWED

        if kind is DocumentKind.IMAGE and not self._gate.ocr:
            return AttachmentVerdict.IMAGE_WITHOUT_OCR

        return AttachmentVerdict.TAKE

    def aspect(self, attachment: Attachment) -> Aspect:
        if self.kind(attachment) is DocumentKind.IMAGE:
            return Aspect.OCR

        return Aspect.BODY

    def consumer(self, attachment: Attachment) -> Callable[[BinaryIO], str]:
        """Потребитель потока скачивания: роутер открывает файл по подсказке
        вложения, текст страниц склеивается в один."""
        hint = self.hint(attachment)

        def read(source: BinaryIO) -> str:
            try:
                with self._router.open(source, hint) as document:
                    return self._join(document.pages(PageWindow.whole()))
            except DocumentError as exc:
                raise DocumentTextError(
                    f"attachment {attachment.id} {attachment.title!r} "
                    f"({attachment.media_type}): {exc}"
                ) from exc

        return read

    def hint(self, attachment: Attachment) -> DocumentHint:
        return DocumentHint(media_type=attachment.media_type, filename=attachment.title)

    def kind(self, attachment: Attachment) -> DocumentKind:
        return Formats.of_hint(self.hint(attachment))

    def _join(self, pages: Iterable[ParsedPage]) -> str:
        return self.PAGE_SEPARATOR.join(self._texts(pages))

    @staticmethod
    def _texts(pages: Iterable[ParsedPage]) -> Iterator[str]:
        for page in pages:
            text = page.text.strip()
            if not text:
                continue

            yield text
