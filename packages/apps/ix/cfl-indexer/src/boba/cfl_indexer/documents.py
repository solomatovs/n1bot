"""Текст вложения: вердикт по маскам и OCR, вид документа по media-type и имени,
извлечение текста роутером boba-doc прямо из потока скачивания.

Ошибки:
DocumentTextError — файл не распознан роутером или не разобран.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import BinaryIO

from boba.cfl_indexer.confluence import Attachment
from boba.cfl_indexer.store import Aspect
from boba.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    AttachmentInfo,
    AttachmentVerdict,
)
from boba.doc import (
    DocumentError,
    DocumentHint,
    DocumentKind,
    DocumentRouter,
    Formats,
    PageWindow,
    ParsedPage,
)

__all__ = [
    "DocumentTextError",
    "aspect_of",
    "build_gate",
    "decide_attachment",
    "text_reader",
]

PAGE_SEPARATOR = "\n\n"


class DocumentTextError(Exception):
    """Текст из файла вложения не извлечён."""


def build_gate(masks: Sequence[str], *, ocr: bool) -> AttachmentGate:
    return AttachmentGate(
        allowed=AttachmentFilter.of_masks(masks), requested=True, ocr=ocr
    )


def hint_of(attachment: Attachment) -> DocumentHint:
    return DocumentHint(media_type=attachment.media_type, filename=attachment.title)


def kind_of(attachment: Attachment) -> DocumentKind:
    return Formats.of_hint(hint_of(attachment))


def decide_attachment(
    attachment: Attachment, gate: AttachmentGate
) -> AttachmentVerdict:
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
    verdict = gate.verdict(info)
    if verdict is not AttachmentVerdict.TAKE:
        return verdict

    kind = kind_of(attachment)
    if kind is DocumentKind.UNKNOWN:
        return AttachmentVerdict.NOT_ALLOWED

    if kind is DocumentKind.IMAGE and not gate.ocr:
        return AttachmentVerdict.IMAGE_WITHOUT_OCR

    return AttachmentVerdict.TAKE


def aspect_of(attachment: Attachment) -> Aspect:
    if kind_of(attachment) is DocumentKind.IMAGE:
        return Aspect.OCR

    return Aspect.BODY


def text_reader(
    router: DocumentRouter, attachment: Attachment
) -> Callable[[BinaryIO], str]:
    """Потребитель потока скачивания: роутер открывает файл по подсказке
    вложения, текст страниц склеивается в один."""
    hint = hint_of(attachment)

    def read(source: BinaryIO) -> str:
        try:
            with router.open(source, hint) as document:
                return join_pages(document.pages(PageWindow.whole()))
        except DocumentError as exc:
            raise DocumentTextError(
                f"attachment {attachment.id} {attachment.title!r} "
                f"({attachment.media_type}): {exc}"
            ) from exc

    return read


def join_pages(pages: Iterable[ParsedPage]) -> str:
    return PAGE_SEPARATOR.join(page_texts(pages))


def page_texts(pages: Iterable[ParsedPage]) -> Iterator[str]:
    for page in pages:
        text = page.text.strip()
        if not text:
            continue

        yield text
