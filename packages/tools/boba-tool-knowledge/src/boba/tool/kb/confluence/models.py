"""Доменные модели Confluence: ошибки, REST-DTO, attachment'ы, metadata-ключи.

Один модуль на весь value-object-слой Confluence-инструмента:

- ConfluencePayloadError        — ошибка разбора REST-ответа.
- ConfluencePageItem/...      — Pydantic-DTO discovery-эндпоинтов.
- AttachmentInfo/Filter       — value-object вложения + allowlist-фильтр
  (+ симметричный JSON-кодек для metadata wire-format на самом AttachmentInfo).
- ConfluenceKeys                — Confluence-специфичные MetadataKey.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from boba.indexing import MetadataKey

__all__ = [
    "AttachmentFilter",
    "AttachmentGate",
    "AttachmentInfo",
    "AttachmentVerdict",
    "ConfluenceDescription",
    "ConfluenceKeys",
    "ConfluencePageItem",
    "ConfluencePayloadError",
    "ConfluencePlainText",
    "ConfluenceSpaceItem",
    "HttpKeys",
]


class ConfluencePayloadError(Exception):
    """Невалидный/нечитаемый JSON-payload от Confluence REST.

    Поднимается decoder'ами и reader'ами при ошибке разбора ответа.
    """


class ConfluencePageItem(BaseModel):
    """Один page-result из Confluence discovery-эндпоинтов.

    Из всех полей discovery нам нужен только id — он передаётся в
    /rest/api/content/{id}?expand=… дальше по pipeline'у. title оставлен
    для логов/диагностики (на cwiki/Atlassian всегда присутствует).
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str = ""


class ConfluencePlainText(BaseModel):
    """Inner description.plain из /rest/api/space?expand=description.plain."""

    model_config = ConfigDict(extra="ignore")

    value: str = ""


class ConfluenceDescription(BaseModel):
    """description вложенный объект space'а с опциональным plain-текстом."""

    model_config = ConfigDict(extra="ignore")

    plain: ConfluencePlainText | None = None


class ConfluenceSpaceItem(BaseModel):
    """Один space-result из /rest/api/space?[type=…][&expand=description.plain].

    description — заполняется только при expand=description.plain. В
    остальных случаях None. Используем property description_plain для
    удобного доступа без .description.plain.value цепочки.
    """

    model_config = ConfigDict(extra="ignore")

    key: str
    name: str = ""
    type: str = ""
    description: ConfluenceDescription | None = None

    @property
    def description_plain(self) -> str:
        if self.description and self.description.plain:
            return self.description.plain.value
        return ""


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """Один attachment Confluence-страницы —
    то, что нужно download'у и rewriter'у ссылок.

    - id             — attachment id (att123…); используется как часть source_id
                         при fan-out'е, и в локальном имени файла как fallback.
    - title          — filename как его показывает Confluence (с расширением).
    - media_type     — MIME (image/png, application/pdf); идёт в
                         TransportKeys.CONTENT_TYPE дочернего request'а,
                         по нему DispatchReader выбирает Reader или skip.
    - file_size      — bytes; 0 если Confluence не отдал.
    - download_path  — relative path от base_url (/download/attachments/…);
                         caller склеивает с base_url чтобы получить полный URL.
    - webui          — relative UI-link вложения (_links.webui), для цитаты:
                         caller склеивает с base_url. "" если Confluence не отдал.
    - version        — version.number; 1 если отсутствует.

    JSON-кодек (encode/decode/encode_many/decode_many) симметричен и
    идемпотентен; схема — объект с теми же именами полей, что у dataclass'а.
    Используется как encode/decode для ConfluenceKeys.ATTACHMENT[S].
    """

    id: str
    title: str
    media_type: str
    file_size: int
    download_path: str
    webui: str
    version: int

    def encode(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def decode(s: str) -> AttachmentInfo:
        return AttachmentInfo._from_dict(json.loads(s))

    @staticmethod
    def encode_many(value: tuple[AttachmentInfo, ...]) -> str:
        return json.dumps([asdict(a) for a in value], ensure_ascii=False)

    @staticmethod
    def decode_many(s: str) -> tuple[AttachmentInfo, ...]:
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
            webui=str(d.get("webui", "")),
            version=int(d.get("version") or 1),
        )


@dataclass(frozen=True, slots=True)
class AttachmentFilter:
    """Allowlist-фильтр attachment'ов по media_type и/или title.

    Семантика:
    - Оба списка пустые -> matches всегда True (бэк-совместимость).
    - Иначе attachment проходит, если совпадает хотя бы с одним паттерном
      из любого непустого списка (OR между списками и внутри списка).
    - Паттерны — fnmatch-globs (*, ?, [abc]); case-insensitive,
      сравнение по lower-case с обеих сторон.

    Примеры:
    - media_type_patterns=("application/pdf",) — только PDF по MIME.
    - title_patterns=("*.pdf", "*.docx") — PDF и DOCX по расширению.
    - media_type_patterns=("image/*",), title_patterns=("*.pdf",)
      — любые картинки ИЛИ файлы с расширением .pdf.
    """

    media_type_patterns: tuple[str, ...] = ()
    title_patterns: tuple[str, ...] = ()

    @classmethod
    def from_lists(
        cls,
        media_types: Iterable[str] = (),
        titles: Iterable[str] = (),
    ) -> AttachmentFilter:
        return cls(
            media_type_patterns=tuple(media_types),
            title_patterns=tuple(titles),
        )

    SEPARATORS: ClassVar[str] = ",;\n\t "
    """Разделители паттернов в строке запроса: LLM пишет их как придётся."""

    MEDIA_MARK: ClassVar[str] = "/"
    """Слэш в паттерне — это media-type, иначе имя файла."""

    @classmethod
    def parse(cls, raw: str) -> AttachmentFilter:
        """Строка паттернов -> фильтр; со слэшем идёт в media-type, прочее в имя."""
        media: list[str] = []
        titles: list[str] = []

        for item in cls._items(raw):
            if cls.MEDIA_MARK in item:
                media.append(item)
                continue

            titles.append(item)

        return cls(media_type_patterns=tuple(media), title_patterns=tuple(titles))

    @classmethod
    def _items(cls, raw: str) -> Iterator[str]:
        chunk = raw
        for separator in cls.SEPARATORS[:-1]:
            chunk = chunk.replace(separator, " ")

        for item in chunk.split(" "):
            cleaned = item.strip()
            if cleaned:
                yield cleaned

    def is_passthrough(self) -> bool:
        return not self.media_type_patterns and not self.title_patterns

    def is_empty(self) -> bool:
        """Ни одного паттерна: запрос вложений не просил."""
        return self.is_passthrough()

    def matches(self, att: AttachmentInfo) -> bool:
        if self.is_passthrough():
            return True
        mt = att.media_type.lower()
        if any(fnmatchcase(mt, p.lower()) for p in self.media_type_patterns):
            return True
        title = att.title.lower()
        return any(fnmatchcase(title, p.lower()) for p in self.title_patterns)


class AttachmentVerdict(StrEnum):
    """Решение по одному вложению; попадает в лог как причина пропуска."""

    TAKE = "take"
    NOT_REQUESTED = "not requested"
    NOT_ALLOWED = "not allowed by config"
    IMAGE_WITHOUT_OCR = "image without ocr"


@dataclass(frozen=True, slots=True)
class AttachmentGate:
    """Что из вложений страницы реально пойдёт в индекс.

    Два фильтра: `allowed` из конфига — потолок администратора, `requested` из
    запроса LLM — выбор внутри потолка. Пустой запрос значит «вложения не
    нужны»: качать их незачем. Картинки без OCR отсекаются отдельно — текста
    из них всё равно не извлечь, а скачивание и разбор стоят времени.
    """

    IMAGE_MEDIA_PREFIX: ClassVar[str] = "image/"

    allowed: AttachmentFilter
    requested: AttachmentFilter
    ocr_enabled: bool

    @classmethod
    def of(
        cls,
        allowed: AttachmentFilter,
        requested: str,
        *,
        ocr_enabled: bool,
    ) -> AttachmentGate:
        return cls(
            allowed=allowed,
            requested=AttachmentFilter.parse(requested),
            ocr_enabled=ocr_enabled,
        )

    def wants_attachments(self) -> bool:
        return not self.requested.is_empty()

    def verdict(self, att: AttachmentInfo) -> AttachmentVerdict:
        if self.requested.is_empty():
            return AttachmentVerdict.NOT_REQUESTED

        if not self.requested.matches(att):
            return AttachmentVerdict.NOT_REQUESTED

        if not self.allowed.matches(att):
            return AttachmentVerdict.NOT_ALLOWED

        if self._is_image(att) and not self.ocr_enabled:
            return AttachmentVerdict.IMAGE_WITHOUT_OCR

        return AttachmentVerdict.TAKE

    @classmethod
    def _is_image(cls, att: AttachmentInfo) -> bool:
        return att.media_type.lower().startswith(cls.IMAGE_MEDIA_PREFIX)


class HttpKeys:
    """HTTP-специфичные ключи metadata, проставляемые при сборке RawDocument."""

    LAST_MODIFIED: ClassVar[MetadataKey[str]] = MetadataKey(
        name="transport.http.last_modified",
        decode=str,
        encode=str,
    )
    STATUS: ClassVar[MetadataKey[int]] = MetadataKey(
        name="transport.http.status",
        decode=int,
        encode=str,
    )


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
    """Canonical URL страницы — тот же wire-ключ source_url, что и у kbdoc."""

    PARENT_URL: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.parent_url",
        decode=str,
        encode=str,
    )
    """URL родительской страницы (её _links.webui) — у вложений: где оно лежит."""

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
