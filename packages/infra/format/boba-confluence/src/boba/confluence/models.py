"""Доменные модели Confluence: ошибки, REST-DTO, вложения, отпечатки, ключи.

Один модуль на весь value-object-слой Confluence-инструмента:

- ConfluencePayloadError      — ошибка разбора REST-ответа.
- ConfluenceContent/...       — Pydantic-DTO ответов content/search и content/{id}.
- AttachmentInfo/Filter/Gate  — вложение, allowlist администратора и решение,
  качать ли его.
- ParseGrade/ConfluenceMarks  — уровень разбора и отпечатки версий для реестра.
- ConfluenceSourceId          — identity страницы и вложения по URL.
- ConfluenceKeys              — Confluence-специфичные MetadataKey.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from enum import IntEnum, StrEnum
from fnmatch import fnmatchcase
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.indexing import MetadataKey, SourceId, SourceMark
from boba.transport.http.profile import HttpConnection

__all__ = [
    "AttachmentBlock",
    "AttachmentFilter",
    "AttachmentGate",
    "AttachmentInfo",
    "AttachmentVerdict",
    "ConfluenceContent",
    "ConfluenceDescription",
    "ConfluenceKeys",
    "ConfluenceMarks",
    "ConfluencePageItem",
    "ConfluencePayloadError",
    "ConfluencePlainText",
    "ConfluenceSourceId",
    "ConfluenceSpaceItem",
    "HttpKeys",
    "ParseGrade",
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


class ConfluenceVersion(BaseModel):
    """Блок version у страницы и вложения."""

    model_config = ConfigDict(extra="ignore")

    number: int = 0
    when: str = ""


class ConfluenceSpaceRef(BaseModel):
    """Ссылка на space внутри контента."""

    model_config = ConfigDict(extra="ignore")

    key: str = ""


class ConfluenceLinks(BaseModel):
    """_links контента: webui страницы, download вложения."""

    model_config = ConfigDict(extra="ignore")

    webui: str = ""
    download: str = ""


class ConfluenceExtensions(BaseModel):
    """extensions вложения: тип и размер файла."""

    model_config = ConfigDict(extra="ignore")

    media_type: str = Field(default="", alias="mediaType")
    file_size: int = Field(default=0, alias="fileSize")


class ConfluenceAttachmentItem(BaseModel):
    """Одно вложение из children.attachment или child/attachment."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    title: str = ""
    version: ConfluenceVersion = Field(default_factory=ConfluenceVersion)
    extensions: ConfluenceExtensions = Field(default_factory=ConfluenceExtensions)
    links: ConfluenceLinks = Field(default_factory=ConfluenceLinks, alias="_links")

    def info(self) -> AttachmentInfo:
        return AttachmentInfo(
            id=self.id,
            title=self.title,
            media_type=self.extensions.media_type,
            file_size=self.extensions.file_size,
            download_path=self.links.download,
            webui=self.links.webui,
            version=self.version.number,
            when=self.version.when,
        )


class AttachmentBlock(BaseModel):
    """Список вложений с окном раскрытия: size == limit значит, что есть ещё."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    results: list[ConfluenceAttachmentItem] = Field(default_factory=list)
    size: int = 0
    limit: int = 0
    links: ConfluenceLinks = Field(default_factory=ConfluenceLinks, alias="_links")

    def truncated(self) -> bool:
        """Раскрытие упёрлось в лимит: полный список надо добирать отдельно."""
        if self.limit <= 0:
            return False

        return self.size >= self.limit


class ConfluenceChildren(BaseModel):
    """children контента; нужен только блок вложений."""

    model_config = ConfigDict(extra="ignore")

    attachment: AttachmentBlock = Field(default_factory=AttachmentBlock)


class ConfluenceAncestor(BaseModel):
    """Предок страницы: нужен заголовок для хлебных крошек."""

    model_config = ConfigDict(extra="ignore")

    title: str = ""


class ConfluenceContent(BaseModel):
    """Страница из content/search или content/{id} с раскрытыми version,
    space, ancestors и children.attachment; body есть только у запроса тела."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    title: str = ""
    version: ConfluenceVersion = Field(default_factory=ConfluenceVersion)
    space: ConfluenceSpaceRef = Field(default_factory=ConfluenceSpaceRef)
    ancestors: list[ConfluenceAncestor] = Field(default_factory=list)
    children: ConfluenceChildren = Field(default_factory=ConfluenceChildren)
    links: ConfluenceLinks = Field(default_factory=ConfluenceLinks, alias="_links")
    body: dict[str, Any] = Field(default_factory=dict)

    def ancestor_titles(self) -> tuple[str, ...]:
        titles: list[str] = []
        for ancestor in self.ancestors:
            title = ancestor.title.strip()
            if title:
                titles.append(title)

        return tuple(titles)

    def body_html(self, body_format: str) -> str:
        block = self.body.get(body_format)
        if not isinstance(block, dict):
            return ""

        return str(block.get("value") or "")


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

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    key: str
    name: str = ""
    type: str = ""
    status: str = ""
    """current или archived: контент архивного спейса поиск Confluence не отдаёт."""

    description: ConfluenceDescription | None = None
    links: ConfluenceLinks = Field(default_factory=ConfluenceLinks, alias="_links")

    @property
    def description_plain(self) -> str:
        if self.description and self.description.plain:
            return self.description.plain.value
        return ""

    def url_at(self, profile: HttpConnection) -> str:
        """Адрес спейса на сервере профиля; без webui — корень сервиса."""
        if not self.links.webui:
            return str(profile.root_url())

        return str(profile.url_of(self.links.webui))


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
    - when           — version.when: дата загрузки этой версии.

    JSON-кодек (encode/decode) симметричен и идемпотентен; схема — объект с
    теми же именами полей, что у dataclass'а. Используется как encode/decode
    для ConfluenceKeys.ATTACHMENT_INFO.
    """

    id: str
    title: str
    media_type: str
    file_size: int
    download_path: str
    webui: str
    version: int
    when: str

    def encode(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def decode(s: str) -> AttachmentInfo:
        return AttachmentInfo._from_dict(json.loads(s))

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
            when=str(d.get("when", "")),
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

    MEDIA_MARK: ClassVar[str] = "/"
    """Слэш в маске — это media-type, иначе имя файла."""

    @classmethod
    def of_masks(cls, masks: Iterable[str]) -> AttachmentFilter:
        """Маски конфига -> фильтр; со слэшем идёт в media-type, прочее в имя."""
        media: list[str] = []
        titles: list[str] = []

        for item in cls._items(masks):
            if cls.MEDIA_MARK in item:
                media.append(item)
                continue

            titles.append(item)

        return cls(media_type_patterns=tuple(media), title_patterns=tuple(titles))

    @staticmethod
    def _items(masks: Iterable[str]) -> Iterator[str]:
        for item in masks:
            cleaned = item.strip()
            if cleaned:
                yield cleaned

    def is_passthrough(self) -> bool:
        return not self.media_type_patterns and not self.title_patterns

    def matches(self, att: AttachmentInfo) -> bool:
        if self.is_passthrough():
            return True
        mt = att.media_type.lower()
        if any(fnmatchcase(mt, p.lower()) for p in self.media_type_patterns):
            return True
        title = att.title.lower()
        return any(fnmatchcase(title, p.lower()) for p in self.title_patterns)


class AttachmentVerdict(StrEnum):
    """Решение по одному вложению; попадает в отчёт как причина пропуска."""

    TAKE = "take"
    NOT_REQUESTED = "not requested"
    NOT_ALLOWED = "not allowed by config"
    IMAGE_WITHOUT_OCR = "image without ocr"

    def skipped(self) -> str:
        """Причина для отметки источника; у взятого вложения её нет."""
        if self is AttachmentVerdict.TAKE:
            return ""

        return self.value


@dataclass(frozen=True, slots=True)
class AttachmentGate:
    """Что из вложений страницы реально пойдёт в индекс.

    `allowed` из конфига — потолок администратора, `requested` — просил ли
    вложения вызов. Картинки без OCR отсекаются отдельно — текста из них всё
    равно не извлечь, а скачивание и разбор стоят времени. Гейт решает только,
    качать ли: существование вложения он не отменяет, и отсечённое вложение
    всё равно отмечается увиденным в реестре.
    """

    IMAGE_MEDIA_PREFIX: ClassVar[str] = "image/"

    allowed: AttachmentFilter
    requested: bool
    ocr: bool

    def verdict(self, att: AttachmentInfo) -> AttachmentVerdict:
        if not self.requested:
            return AttachmentVerdict.NOT_REQUESTED

        if not self.allowed.matches(att):
            return AttachmentVerdict.NOT_ALLOWED

        if self._is_image(att) and not self.ocr:
            return AttachmentVerdict.IMAGE_WITHOUT_OCR

        return AttachmentVerdict.TAKE

    @classmethod
    def _is_image(cls, att: AttachmentInfo) -> bool:
        return att.media_type.lower().startswith(cls.IMAGE_MEDIA_PREFIX)


class ParseGrade(IntEnum):
    """Уровень разбора вложения; OCR выше текстового слоя и не откатывается."""

    TEXT = 0
    OCR = 1

    @classmethod
    def of(cls, *, ocr: bool) -> ParseGrade:
        if ocr:
            return cls.OCR

        return cls.TEXT


class ConfluenceMarks:
    """Отпечатки версий для реестра: что известно из списка без скачивания."""

    @staticmethod
    def page(version: int) -> SourceMark:
        return SourceMark(fingerprint=f"v{version}")

    @staticmethod
    def attachment(
        att: AttachmentInfo, *, parent: SourceId, grade: ParseGrade, skip: str = ""
    ) -> SourceMark:
        fingerprint = f"v{att.version}:{att.when}:{att.file_size}:{att.media_type}"
        return SourceMark(
            fingerprint=fingerprint,
            grade=int(grade),
            parent=parent,
            skip=skip,
        )


class ConfluenceSourceId:
    """Identity источника: URL запрошенного объекта без query и фрагмента.

    Страница — её REST-адрес content/{id}, вложение — путь download; один
    объект во всех версиях имеет один id, версия у Confluence живёт в query.
    """

    PAGE_RE: ClassVar[re.Pattern[str]] = re.compile(r"/rest/api/content/([^/?#]+)$")
    ATTACHMENT_MARK: ClassVar[str] = "/download/attachments/"

    @classmethod
    def of(cls, profile: HttpConnection, path: str) -> SourceId:
        return cls.of_url(str(profile.url_of(path)))

    @staticmethod
    def of_url(url: str) -> SourceId:
        """URL без query, фрагмента и учётных данных адреса."""
        bare = httpx.URL(url).copy_with(userinfo=b"", query=None, fragment=None)
        return SourceId(str(bare))

    @classmethod
    def page_id_of(cls, source_id: SourceId) -> str | None:
        """id страницы из её source_id; None, если это не страница."""
        match = cls.PAGE_RE.search(str(source_id))
        if match is None:
            return None

        return match.group(1)

    @classmethod
    def is_attachment(cls, source_id: SourceId) -> bool:
        return cls.ATTACHMENT_MARK in str(source_id)

    @classmethod
    def page_ids_of(cls, source_ids: Iterable[SourceId]) -> Sequence[str]:
        ids: list[str] = []
        for source_id in source_ids:
            page_id = cls.page_id_of(source_id)
            if page_id is not None:
                ids.append(page_id)

        return ids


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
    ATTACHMENT_INFO: ClassVar[MetadataKey[AttachmentInfo]] = MetadataKey(
        name="confluence.attachment_info",
        decode=AttachmentInfo.decode,
        encode=AttachmentInfo.encode,
    )
