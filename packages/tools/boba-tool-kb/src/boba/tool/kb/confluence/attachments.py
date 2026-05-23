"""Confluence attachment value-object + JSON-кодек для metadata.

`AttachmentInfo` — typed snapshot одного вложения, как оно приходит из
`children.attachment.results[]` Confluence REST. Лежит в metadata основной
страницы под `ConfluenceKeys.ATTACHMENTS` как `tuple[AttachmentInfo, ...]`;
дальше используется для fan-out'а `iter_confluence_documents` (1 page → N
attachment-requests) и для переписывания `<img src>` / `<a href>` в HTML
на локальные пути при offline-сохранении.

`AttachmentFilter` — allowlist по `media_type` и `title` (fnmatch-globs),
применяется на fan-out стадии: если фильтр сконфигурирован, attachment
обязан совпасть хотя бы с одним паттерном; иначе он не запрашивается
вообще (никакого HTTP). Дефолтный пустой фильтр пропускает всё —
старое поведение.

JSON-кодек симметричен (`encode → decode → encode` идемпотентно); схема
сериализации — массив объектов с теми же именами полей, что у dataclass'а.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from typing import Any

__all__ = [
    "AttachmentFilter",
    "AttachmentInfo",
    "decode_attachment",
    "decode_attachments",
    "encode_attachment",
    "encode_attachments",
]


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """Один attachment Confluence-страницы —
    то, что нужно download'у и rewriter'у ссылок.

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


@dataclass(frozen=True, slots=True)
class AttachmentFilter:
    """Allowlist-фильтр attachment'ов по `media_type` и/или `title`.

    Семантика:
    - Оба списка пустые → `matches` всегда True (бэк-совместимость).
    - Иначе attachment проходит, если совпадает хотя бы с одним паттерном
      из любого непустого списка (OR между списками и внутри списка).
    - Паттерны — `fnmatch`-globs (`*`, `?`, `[abc]`); case-insensitive,
      сравнение по lower-case с обеих сторон.

    Примеры:
    - `media_type_patterns=("application/pdf",)` — только PDF по MIME.
    - `title_patterns=("*.pdf", "*.docx")` — PDF и DOCX по расширению.
    - `media_type_patterns=("image/*",), title_patterns=("*.pdf",)`
      — любые картинки ИЛИ файлы с расширением `.pdf`.
    """

    media_type_patterns: tuple[str, ...] = ()
    title_patterns: tuple[str, ...] = ()

    @classmethod
    def from_lists(
        cls,
        media_types: Iterable[str] = (),
        titles: Iterable[str] = (),
    ) -> AttachmentFilter:
        """Конструктор из любых итерируемых (list/tuple) — для cfg-полей."""
        return cls(
            media_type_patterns=tuple(media_types),
            title_patterns=tuple(titles),
        )

    def is_passthrough(self) -> bool:
        """True, если фильтр пустой — fan-out пропускает все attachment'ы."""
        return not self.media_type_patterns and not self.title_patterns

    def matches(self, att: AttachmentInfo) -> bool:
        """True, если attachment проходит фильтр (или фильтр пустой)."""
        if self.is_passthrough():
            return True
        mt = att.media_type.lower()
        if any(fnmatchcase(mt, p.lower()) for p in self.media_type_patterns):
            return True
        title = att.title.lower()
        return any(fnmatchcase(title, p.lower()) for p in self.title_patterns)


def encode_attachments(value: tuple[AttachmentInfo, ...]) -> str:
    """`tuple[AttachmentInfo, ...]` → JSON-массив объектов (для Metadata wire-format)"""
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
