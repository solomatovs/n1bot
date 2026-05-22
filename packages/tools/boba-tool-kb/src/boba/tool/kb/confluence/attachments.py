"""Confluence attachment value-object + JSON-кодек для metadata.

`AttachmentInfo` — typed snapshot одного вложения, как оно приходит из
`children.attachment.results[]` Confluence REST. Лежит в metadata основной
страницы под `ConfluenceKeys.ATTACHMENTS` как `tuple[AttachmentInfo, ...]`;
дальше используется для fan-out'а `iter_confluence_documents` (1 page → N
attachment-requests) и для переписывания `<img src>` / `<a href>` в HTML
на локальные пути при offline-сохранении.

JSON-кодек симметричен (`encode → decode → encode` идемпотентно); схема
сериализации — массив объектов с теми же именами полей, что у dataclass'а.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

__all__ = [
    "AttachmentInfo",
    "decode_attachment",
    "decode_attachments",
    "encode_attachment",
    "encode_attachments",
]


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """Один attachment Confluence-страницы — то, что нужно download'у и rewriter'у ссылок.

    - `id`             — attachment id (`att123…`); используется как часть `source_id`
                         при fan-out'е, и в локальном имени файла как fallback.
    - `title`          — filename как его показывает Confluence (с расширением).
    - `media_type`     — MIME (`image/png`, `application/pdf`); идёт в
                         `TransportKeys.CONTENT_TYPE` дочернего request'а,
                         по нему DispatchReader выбирает Reader или skip.
    - `file_size`      — bytes; 0 если Confluence не отдал.
    - `download_path`  — relative path от base_url (`/download/attachments/…`);
                         caller склеивает с `base_url` чтобы получить полный URL.
    - `version`        — version.number; 1 если отсутствует.
    """

    id: str
    title: str
    media_type: str
    file_size: int
    download_path: str
    version: int


def encode_attachments(value: tuple[AttachmentInfo, ...]) -> str:
    """`tuple[AttachmentInfo, ...]` → JSON-массив объектов (для Metadata wire-format)."""
    return json.dumps([asdict(a) for a in value], ensure_ascii=False)


def decode_attachments(s: str) -> tuple[AttachmentInfo, ...]:
    """JSON-массив объектов → `tuple[AttachmentInfo, ...]`.

    Поля, отсутствующие в JSON, получают defaults (`""` для строк, `0` для int,
    `1` для version) — нужно, чтобы старые/обрезанные wire-payload'ы не
    взрывали загрузку. Лишние поля игнорируются.
    """
    items: list[dict[str, Any]] = json.loads(s)
    return tuple(_from_dict(d) for d in items)


def encode_attachment(value: AttachmentInfo) -> str:
    """Один `AttachmentInfo` → JSON-объект.

    Используется для `ConfluenceKeys.ATTACHMENT_INFO` — этот ключ ставится
    Transport'ом на дочернем `RawDocument` (один вложение = один документ).
    """
    return json.dumps(asdict(value), ensure_ascii=False)


def decode_attachment(s: str) -> AttachmentInfo:
    """JSON-объект → `AttachmentInfo`. Симметричен `encode_attachment`."""
    return _from_dict(json.loads(s))


def _from_dict(d: dict[str, Any]) -> AttachmentInfo:
    return AttachmentInfo(
        id=str(d.get("id", "")),
        title=str(d.get("title", "")),
        media_type=str(d.get("media_type", "")),
        file_size=int(d.get("file_size") or 0),
        download_path=str(d.get("download_path", "")),
        version=int(d.get("version") or 1),
    )
