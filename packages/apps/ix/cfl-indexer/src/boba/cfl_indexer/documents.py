"""Текст вложения: вердикт по маскам и OCR, декодирование текстовых файлов, разбор
остальных liteparse прямо в процессе спейса.

Ошибки:
DocumentTextError — файл не декодирован или не разобран.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from boba.cfl_indexer.confluence import Attachment
from boba.cfl_indexer.store import Aspect
from boba.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    AttachmentInfo,
    AttachmentVerdict,
)
from boba.liteparse.engine import LiteParseEngine
from boba.text.document import DocumentMedia, LiteParseError, LiteParseParams
from boba.text.reader import TextMedia

__all__ = [
    "DocumentTextError",
    "aspect_of",
    "build_gate",
    "decide_attachment",
    "extract_text",
]

PAGE_SEPARATOR = "\n\n"
IMAGE_PREFIX = "image/"


class DocumentTextError(Exception):
    """Текст из файла вложения не извлечён."""


def build_gate(masks: Sequence[str], *, ocr: bool) -> AttachmentGate:
    return AttachmentGate(
        allowed=AttachmentFilter.of_masks(masks), requested=True, ocr=ocr
    )


def is_supported(media_type: str) -> bool:
    normalized = DocumentMedia.normalize(media_type)
    if normalized in TextMedia.DOC_TYPE_BY_MEDIA_TYPE:
        return True

    return normalized in DocumentMedia.SUFFIX_BY_MEDIA_TYPE


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

    if not is_supported(attachment.media_type):
        return AttachmentVerdict.NOT_ALLOWED

    return AttachmentVerdict.TAKE


def aspect_of(media_type: str) -> Aspect:
    if DocumentMedia.normalize(media_type).startswith(IMAGE_PREFIX):
        return Aspect.OCR

    return Aspect.BODY


def extract_text(
    path: Path,
    attachment: Attachment,
    encodings: Sequence[str],
    parser: LiteParseParams,
) -> str:
    """Текст файла: текстовые типы декодируются, остальные разбирает liteparse."""
    normalized = DocumentMedia.normalize(attachment.media_type)
    if normalized in TextMedia.DOC_TYPE_BY_MEDIA_TYPE:
        return decode_text(path, attachment, encodings)

    return parse_document(path, attachment, parser)


def decode_text(path: Path, attachment: Attachment, encodings: Sequence[str]) -> str:
    raw = path.read_bytes()
    for encoding in encodings:
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue

    raise DocumentTextError(
        f"attachment {attachment.id} {attachment.title!r}: cannot decode "
        f"{len(raw)} bytes with any of: {', '.join(encodings)}"
    )


def parse_document(path: Path, attachment: Attachment, parser: LiteParseParams) -> str:
    """liteparse узнаёт формат по расширению, поэтому файл на время разбора
    получает жёсткую ссылку с нужным суффиксом."""
    suffix = DocumentMedia.suffix_for(attachment.media_type)
    linked = path.with_name(f"{path.stem}-as{suffix}")
    try:
        os.link(path, linked)
        result = LiteParseEngine.parse_native(parser, str(linked))
    except (LiteParseError, OSError) as exc:
        raise DocumentTextError(
            f"attachment {attachment.id} {attachment.title!r} "
            f"({attachment.media_type}): {exc}"
        ) from exc
    finally:
        linked.unlink(missing_ok=True)

    pages: list[str] = []
    for page in result.pages:
        text = str(page.text).strip()
        if text:
            pages.append(text)

    return PAGE_SEPARATOR.join(pages)
