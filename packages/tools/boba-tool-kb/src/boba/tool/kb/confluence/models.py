"""Доменные модели Confluence: ошибки, REST-DTO, attachment'ы, metadata-ключи.

Один модуль на весь value-object-слой Confluence-инструмента:

- `ConfluencePayloadError`        — ошибка разбора REST-ответа.
- `ConfluencePageItem`/`...`      — Pydantic-DTO discovery-эндпоинтов.
- `AttachmentInfo`/`Filter`       — value-object вложения + allowlist-фильтр
  (+ симметричный JSON-кодек для metadata wire-format на самом `AttachmentInfo`).
- `ConfluenceKeys`                — Confluence-специфичные `MetadataKey`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from boba.indexing import MetadataKey

__all__ = [
    "AttachmentFilter",
    "AttachmentInfo",
    "ConfluenceDescription",
    "ConfluenceKeys",
    "ConfluencePageItem",
    "ConfluencePayloadError",
    "ConfluencePlainText",
    "ConfluenceSpaceItem",
]


class ConfluencePayloadError(Exception):
    """Невалидный/нечитаемый JSON-payload от Confluence REST.

    Поднимается decoder'ами и reader'ами при ошибке разбора ответа.
    """


class ConfluencePageItem(BaseModel):
    """Один page-result из Confluence discovery-эндпоинтов.

    Из всех полей discovery нам нужен только `id` — он передаётся в
    `/rest/api/content/{id}?expand=…` дальше по pipeline'у. `title` оставлен
    для логов/диагностики (на cwiki/Atlassian всегда присутствует).
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str = ""


class ConfluencePlainText(BaseModel):
    """Inner `description.plain` из `/rest/api/space?expand=description.plain`."""

    model_config = ConfigDict(extra="ignore")

    value: str = ""


class ConfluenceDescription(BaseModel):
    """`description` вложенный объект space'а с опциональным plain-текстом."""

    model_config = ConfigDict(extra="ignore")

    plain: ConfluencePlainText | None = None


class ConfluenceSpaceItem(BaseModel):
    """Один space-result из `/rest/api/space?[type=…][&expand=description.plain]`.

    `description` — заполняется только при `expand=description.plain`. В
    остальных случаях None. Используем property `description_plain` для
    удобного доступа без `.description.plain.value` цепочки.
    """

    model_config = ConfigDict(extra="ignore")

    key: str
    name: str = ""
    type: str = ""
    description: ConfluenceDescription | None = None

    @property
    def description_plain(self) -> str:
        """Plain-text описание space'а или пустая строка."""
        if self.description and self.description.plain:
            return self.description.plain.value
        return ""


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

    JSON-кодек (`encode`/`decode`/`encode_many`/`decode_many`) симметричен и
    идемпотентен; схема — объект с теми же именами полей, что у dataclass'а.
    Используется как `encode`/`decode` для `ConfluenceKeys.ATTACHMENT[S]`.
    """

    id: str
    title: str
    media_type: str
    file_size: int
    download_path: str
    version: int

    def encode(self) -> str:
        """Один `AttachmentInfo` → JSON-объект (для `ATTACHMENT_INFO`)."""
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def decode(s: str) -> AttachmentInfo:
        """JSON-объект → `AttachmentInfo`. Симметричен `encode`."""
        return AttachmentInfo._from_dict(json.loads(s))

    @staticmethod
    def encode_many(value: tuple[AttachmentInfo, ...]) -> str:
        """`tuple[AttachmentInfo, ...]` → JSON-массив (для `ATTACHMENTS`)."""
        return json.dumps([asdict(a) for a in value], ensure_ascii=False)

    @staticmethod
    def decode_many(s: str) -> tuple[AttachmentInfo, ...]:
        """JSON-массив объектов → `tuple[AttachmentInfo, ...]`.

        Поля, отсутствующие в JSON, получают defaults (`""` для строк, `0` для int,
        `1` для version) — нужно, чтобы старые/обрезанные wire-payload'ы не
        взрывали загрузку. Лишние поля игнорируются.
        """
        items: list[dict[str, Any]] = json.loads(s)
        return tuple(AttachmentInfo._from_dict(d) for d in items)

    @staticmethod
    def _from_dict(d: dict[str, Any]) -> AttachmentInfo:
        return AttachmentInfo(
            id=str(d.get("id", "")),
            title=str(d.get("title", "")),
            media_type=str(d.get("media_type", "")),
            file_size=int(d.get("file_size") or 0),
            download_path=str(d.get("download_path", "")),
            version=int(d.get("version") or 1),
        )


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


class ConfluenceKeys:
    """Confluence-специфичные ключи metadata."""

    @staticmethod
    def _decode_titles(s: str) -> tuple[str, ...]:
        return tuple(str(x) for x in json.loads(s))

    @staticmethod
    def _encode_titles(v: tuple[str, ...]) -> str:
        return json.dumps(list(v), ensure_ascii=False)

    SOURCE_URL: ClassVar[MetadataKey[str]] = MetadataKey(
        name="source_url",
        decode=str,
        encode=str,
    )
    """Canonical URL страницы — тот же wire-ключ `source_url`, что и у kbdoc."""

    PAGE_ID: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.page_id",
        decode=str,
        encode=str,
    )
    HOST: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.host",
        decode=str,
        encode=str,
    )
    VERSION: ClassVar[MetadataKey[int]] = MetadataKey(
        name="confluence.version",
        decode=int,
        encode=str,
    )
    SPACE_KEY: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.space_key",
        decode=str,
        encode=str,
    )
    ANCESTORS_TITLES: ClassVar[MetadataKey[tuple[str, ...]]] = MetadataKey(
        name="confluence.ancestors_titles",
        decode=_decode_titles,
        encode=_encode_titles,
    )
    ATTACHMENTS: ClassVar[MetadataKey[tuple[AttachmentInfo, ...]]] = MetadataKey(
        name="confluence.attachments",
        decode=AttachmentInfo.decode_many,
        encode=AttachmentInfo.encode_many,
    )
    ATTACHMENT_INFO: ClassVar[MetadataKey[AttachmentInfo]] = MetadataKey(
        name="confluence.attachment_info",
        decode=AttachmentInfo.decode,
        encode=AttachmentInfo.encode,
    )
