"""Доменные модели Confluence: ошибки, REST-DTO, вложения, отпечатки, ключи.

Один модуль на весь value-object-слой Confluence-инструмента:

- ConfluencePayloadError      — ошибка разбора REST-ответа.
- ConfluenceContent/...       — Pydantic-DTO ответов content/search и content/{id}.
- AttachmentInfo/Filter/Gate  — вложение, allowlist администратора и решение,
  качать ли его.
- ParseGrade/ConfluenceMarks  — уровень разбора и отпечатки версий для реестра.
- ConfluenceSourceIds         — identity страницы и вложения по URL.
- ConfluenceKeys              — Confluence-специфичные MetadataKey.
- PageSections/PageSection    — результат разбора страницы: карточка, текст
  под заголовками и таблицы; контракт между разбором HTML и ридером.
- TableShape                  — пороги, по которым таблица раскладывается
  построчно или сеткой.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from fnmatch import fnmatchcase
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.indexing import MetadataKey, SourceId, SourceMark, TableLayout
from boba.transport.http.connection import HttpConnection

__all__ = [
    "AttachmentBlock",
    "AttachmentFilter",
    "AttachmentGate",
    "AttachmentInfo",
    "AttachmentVerdict",
    "ConfluenceContainer",
    "ConfluenceContent",
    "ConfluenceContentExtensions",
    "ConfluenceDescription",
    "ConfluenceHistory",
    "ConfluenceKeys",
    "ConfluenceLabel",
    "ConfluenceLabels",
    "ConfluenceMarks",
    "ConfluenceMetadata",
    "ConfluencePageItem",
    "ConfluencePayloadError",
    "ConfluencePlainText",
    "ConfluenceSourceIds",
    "ConfluenceSpaceItem",
    "ConfluenceUser",
    "HttpKeys",
    "LinkKind",
    "PageCardSection",
    "PageHref",
    "PageLink",
    "PageOutlineItem",
    "PageParseRequest",
    "PageSection",
    "PageSectionBase",
    "PageSectionKind",
    "PageSections",
    "PageTableSection",
    "PageTarget",
    "PageTextSection",
    "ParseGrade",
    "SpaceMask",
    "TableShape",
    "TitlesCodec",
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


class ConfluenceUser(BaseModel):
    """Пользователь Confluence в version.by и history.createdBy."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    username: str = ""
    display_name: str = Field(default="", alias="displayName")


class ConfluenceVersion(BaseModel):
    """Блок version у страницы и вложения."""

    model_config = ConfigDict(extra="ignore")

    number: int = 0
    when: str = ""
    by: ConfluenceUser = Field(default_factory=ConfluenceUser)


class ConfluenceHistory(BaseModel):
    """Блок history контента: когда и кем создан (expand=history)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    created_date: str = Field(default="", alias="createdDate")
    created_by: ConfluenceUser = Field(
        default_factory=ConfluenceUser, alias="createdBy"
    )


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


class ConfluenceContentExtensions(BaseModel):
    """extensions контента: у комментария здесь место — inline или footer.

    Server отдаёт location строкой, Cloud — списком; наружу всегда строка.
    """

    model_config = ConfigDict(extra="ignore")

    location: str = ""

    @field_validator("location", mode="before")
    @classmethod
    def _first_of_list(cls, value: object) -> object:
        if isinstance(value, list):
            if not value:
                return ""

            return str(value[0])

        return value


class ConfluenceContainer(BaseModel):
    """container контента: страница, к которой относится комментарий."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""


class ConfluenceAncestor(BaseModel):
    """Предок страницы: нужен заголовок для хлебных крошек."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    title: str = ""


class ConfluenceLabel(BaseModel):
    """Метка страницы из expand=metadata.labels."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""


class ConfluenceLabels(BaseModel):
    """Список меток внутри metadata.labels."""

    model_config = ConfigDict(extra="ignore")

    results: list[ConfluenceLabel] = Field(default_factory=list)


class ConfluenceMetadata(BaseModel):
    """metadata страницы; из всего блока нужны только labels."""

    model_config = ConfigDict(extra="ignore")

    labels: ConfluenceLabels = Field(default_factory=ConfluenceLabels)


class ConfluenceContent(BaseModel):
    """Страница из content/search или content/{id} с раскрытыми version,
    space, ancestors, metadata.labels и children.attachment; body есть только
    у запроса тела."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    title: str = ""
    type: str = ""
    status: str = ""
    version: ConfluenceVersion = Field(default_factory=ConfluenceVersion)
    history: ConfluenceHistory = Field(default_factory=ConfluenceHistory)
    space: ConfluenceSpaceRef = Field(default_factory=ConfluenceSpaceRef)
    extensions: ConfluenceContentExtensions = Field(
        default_factory=ConfluenceContentExtensions
    )
    container: ConfluenceContainer = Field(default_factory=ConfluenceContainer)
    ancestors: list[ConfluenceAncestor] = Field(default_factory=list)
    children: ConfluenceChildren = Field(default_factory=ConfluenceChildren)
    metadata: ConfluenceMetadata = Field(default_factory=ConfluenceMetadata)
    links: ConfluenceLinks = Field(default_factory=ConfluenceLinks, alias="_links")
    body: dict[str, Any] = Field(default_factory=dict)

    def ancestor_titles(self) -> tuple[str, ...]:
        titles: list[str] = []
        for ancestor in self.ancestors:
            title = ancestor.title.strip()
            if title:
                titles.append(title)

        return tuple(titles)

    def label_names(self) -> tuple[str, ...]:
        """Метки страницы; пустые и повторы отброшены."""
        names: list[str] = []
        for label in self.metadata.labels.results:
            name = label.name.strip()
            if not name:
                continue

            if name in names:
                continue

            names.append(name)

        return tuple(names)

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

    def url_at(self, connection: HttpConnection) -> str:
        """Адрес спейса на сервере профиля; без webui — корень сервиса."""
        if not self.links.webui:
            return str(connection.root_url())

        return str(connection.url_of(self.links.webui))


class AttachmentInfo(BaseModel):
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

    В metadata чанков хранится JSON модели (ConfluenceKeys.ATTACHMENT_INFO);
    поля с умолчаниями терпят записи старых раскладок без части ключей.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = ""
    title: str = ""
    media_type: str = ""
    file_size: int = 0
    download_path: str = ""
    webui: str = ""
    version: int = 1
    when: str = ""


class AttachmentFilter:
    """Allowlist-фильтр вложений по media_type и/или имени файла.

    Строится из масок конфига: маска со слэшем — media-type, без него — имя
    файла; пустые маски отбрасываются. Без масок проходит всё, иначе вложение
    проходит, если совпало хотя бы с одной маской любого списка. Маски —
    fnmatch-globs (*, ?, [abc]) без учёта регистра.
    """

    MEDIA_MARK: ClassVar[str] = "/"
    """Слэш в маске — это media-type, иначе имя файла."""

    def __init__(self, masks: Iterable[str]) -> None:
        media: list[str] = []
        titles: list[str] = []
        for item in self._items(masks):
            if self.MEDIA_MARK in item:
                media.append(item)
                continue

            titles.append(item)

        self._media_type_patterns = tuple(media)
        self._title_patterns = tuple(titles)

    @property
    def media_type_patterns(self) -> tuple[str, ...]:
        return self._media_type_patterns

    @property
    def title_patterns(self) -> tuple[str, ...]:
        return self._title_patterns

    @staticmethod
    def _items(masks: Iterable[str]) -> Iterator[str]:
        for item in masks:
            cleaned = item.strip()
            if cleaned:
                yield cleaned

    def is_passthrough(self) -> bool:
        return not self._media_type_patterns and not self._title_patterns

    def matches(self, att: AttachmentInfo) -> bool:
        if self.is_passthrough():
            return True

        media_type = att.media_type.lower()
        for pattern in self._media_type_patterns:
            if fnmatchcase(media_type, pattern.lower()):
                return True

        title = att.title.lower()
        for pattern in self._title_patterns:
            if fnmatchcase(title, pattern.lower()):
                return True

        return False


class SpaceMask:
    """Маски выбора спейсов: ключ как есть или glob по ключу и названию.

    Собирается один раз в конструкторе владельца (читатель спейсов, список
    спейсов инструмента) из масок конфига или вызова: пустые и пробельные
    маски отбрасываются. Список без glob-символов это перечисление ключей, и
    список спейсов с сервера для него не нужен; маска со звёздочкой требует
    обхода списка, поэтому вызывающий спрашивает has_wildcard до запроса.
    """

    GLOB_MARKS: ClassVar[str] = "*?["

    def __init__(self, masks: Iterable[str]) -> None:
        patterns: list[str] = []
        for item in masks:
            cleaned = item.strip()
            if cleaned:
                patterns.append(cleaned)

        self._patterns = tuple(patterns)

    @property
    def patterns(self) -> tuple[str, ...]:
        return self._patterns

    @property
    def has_wildcard(self) -> bool:
        for pattern in self._patterns:
            if any(mark in pattern for mark in self.GLOB_MARKS):
                return True

        return False

    def is_passthrough(self) -> bool:
        """Без масок проходит любой спейс."""
        return not self._patterns

    def as_keys(self) -> tuple[str, ...]:
        """Маски как ключи: годится, только когда has_wildcard ложно."""
        return self._patterns

    def matches(self, space: ConfluenceSpaceItem) -> bool:
        """Совпадение по ключу или названию целиком, без учёта регистра;
        без масок совпадает всё."""
        if self.is_passthrough():
            return True

        key = space.key.lower()
        name = space.name.lower()
        for pattern in self._patterns:
            lowered = pattern.lower()
            if fnmatchcase(key, lowered):
                return True

            if fnmatchcase(name, lowered):
                return True

        return False


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

    def _is_image(self, att: AttachmentInfo) -> bool:
        return att.media_type.lower().startswith(self.IMAGE_MEDIA_PREFIX)


class PageSectionKind(StrEnum):
    """Вид записи разбора страницы."""

    TEXT = "text"
    TABLE = "table"
    CARD = "card"


class TableShape(BaseModel):
    """Пороги выбора раскладки таблицы.

    Узкая и длинная таблица — справочник: в ней ищут одну строку, поэтому
    она раскладывается построчно. Широкая или короткая остаётся сеткой с
    повторяемой шапкой. Решение принимает модель, чтобы порог не разъехался
    между разбором и тестами.
    """

    model_config = ConfigDict(extra="forbid")

    row_layout_max_columns: int = Field(
        ge=1,
        description=(
            "Колонок не больше — таблица считается справочником и режется "
            "построчно записями «колонка: значение»."
        ),
    )
    row_layout_min_rows: int = Field(
        ge=1,
        description=(
            "Строк не меньше — иначе таблица короткая и целиком влезает "
            "в один чанк сеткой."
        ),
    )

    def layout_for(self, *, columns: int, rows: int) -> TableLayout:
        if columns > self.row_layout_max_columns:
            return TableLayout.GRID

        if rows < self.row_layout_min_rows:
            return TableLayout.GRID

        return TableLayout.ROWS


class PageParseRequest(BaseModel):
    """Вход разбора страницы: тело, заголовок и пороги раскладки таблиц."""

    model_config = ConfigDict(extra="forbid")

    html: str
    title: str = ""
    page_id: str = Field(min_length=1)
    table_shape: TableShape


@dataclass(frozen=True)
class PageTarget:
    """Страница, на которую ведёт ссылка: id и/или заголовок из адреса.

    Короткая ссылка /x/<код> не несёт ни того, ни другого — для неё оба
    поля пусты, и подпись берётся из текста ссылки.
    """

    page_id: str = ""
    title: str = ""

    def is_page(self, *, page_id: str, title: str) -> bool:
        """Ссылка ведёт на страницу с этим id или заголовком."""
        if self.page_id and self.page_id == page_id:
            return True

        if self.title and self.title == title:  # noqa: SIM103
            return True

        return False


class LinkKind(StrEnum):
    """Как ссылка на страницу записана в теле: по id, по заголовку или макросом."""

    ID = "id"
    TITLE = "title"
    MACRO = "macro"


@dataclass(frozen=True)
class PageLink:
    """Ссылка со страницы на другую страницу: цель и способ записи."""

    target: PageTarget
    kind: LinkKind


class PageHref:
    """Ссылка href и страница Confluence за ней — одна точка, где живут формы
    адресов.

    Создаётся разбором HTML на каждую ссылку; target() отдаёт страницу или None,
    если это не другая страница Confluence. Страницей считаются
    /spaces/<KEY>/pages/<id>/<Title>, /display/<SPACE>/<Title>,
    /pages/viewpage.action?pageId=<id> и короткие /x/<код>. Профили
    (/display/~user), вложения, черновики, фрагменты своей страницы и внешние
    адреса страницами не являются.
    """

    SPACES_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"/spaces/[^/]+/pages/(\d+)(?:/([^/]*))?/?$"
    )
    DISPLAY_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"/display/[^/~][^/]*/([^/]+)/?$"
    )
    VIEWPAGE_RE: ClassVar[re.Pattern[str]] = re.compile(r"/pages/viewpage\.action$")
    TINY_RE: ClassVar[re.Pattern[str]] = re.compile(r"/x/[^/]+/?$")
    PAGE_ID_PARAM: ClassVar[str] = "pageId"

    def __init__(self, href: str) -> None:
        self._parts = urlsplit(href)

    def target(self) -> PageTarget | None:
        """Цель ссылки или None, если это не другая страница Confluence."""
        if not self._parts.path:
            return None

        probes = (self._spaces, self._viewpage, self._display, self._tiny)
        for probe in probes:
            target = probe(self._parts)
            if target is not None:
                return target

        return None

    def _spaces(self, parts: SplitResult) -> PageTarget | None:
        match = self.SPACES_RE.search(parts.path)
        if match is None:
            return None

        segment = match.group(2)
        if segment is None:
            segment = ""

        return PageTarget(page_id=match.group(1), title=self._title(segment))

    def _viewpage(self, parts: SplitResult) -> PageTarget | None:
        if not self.VIEWPAGE_RE.search(parts.path):
            return None

        ids = parse_qs(parts.query).get(self.PAGE_ID_PARAM)
        if not ids:
            return None

        return PageTarget(page_id=ids[0])

    def _display(self, parts: SplitResult) -> PageTarget | None:
        match = self.DISPLAY_RE.search(parts.path)
        if match is None:
            return None

        return PageTarget(title=self._title(match.group(1)))

    def _tiny(self, parts: SplitResult) -> PageTarget | None:
        if not self.TINY_RE.search(parts.path):
            return None

        return PageTarget()

    @staticmethod
    def _title(segment: str) -> str:
        """Сегмент адреса в заголовок: `+` — пробел, `%2B` — сам плюс."""
        return unquote(segment.replace("+", " ")).strip()


class PageOutlineItem(BaseModel):
    """Строка оглавления страницы: уровень заголовка, текст и якорь."""

    model_config = ConfigDict(extra="forbid")

    level: int = Field(ge=0)
    text: str
    anchor: str = ""


class PageSectionBase(BaseModel):
    """Общие поля записи разбора: место в документе и локус цитирования."""

    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=0)
    heading_path: str = ""
    anchor: str = ""


class PageTextSection(PageSectionBase):
    """Текст под заголовком; таблицы из него вынуты отдельными записями."""

    kind: Literal[PageSectionKind.TEXT] = PageSectionKind.TEXT
    content: str
    heading_level: int = Field(default=0, ge=0)
    heading_text: str = ""


class PageTableSection(PageSectionBase):
    """Таблица страницы с разобранной шапкой и строками."""

    kind: Literal[PageSectionKind.TABLE] = PageSectionKind.TABLE
    caption: str = ""
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    layout: TableLayout = TableLayout.GRID


class PageCardSection(PageSectionBase):
    """Карточка страницы: что за страница, из чего состоит, на что ссылается.

    Метки и хлебные крошки сюда не входят — они приходят не из HTML, а из
    ответа REST, и их добавляет ридер при сборке доменной секции.
    """

    kind: Literal[PageSectionKind.CARD] = PageSectionKind.CARD
    title: str = ""
    outline: tuple[PageOutlineItem, ...] = ()
    links: tuple[str, ...] = ()


PageSection = Annotated[
    PageTextSection | PageTableSection | PageCardSection,
    Field(discriminator="kind"),
]


class PageSections(BaseModel):
    """Результат разбора страницы — контракт между разбором и ридером.

    Разбор (bs4 живёт только в нём) отдаёт model_dump, ридер конвейера
    валидирует обратно: расхождение полей падает один раз в точке разбора,
    а не всплывает отсутствующим ключом посреди индексации.
    """

    model_config = ConfigDict(extra="forbid")

    sections: tuple[PageSection, ...] = ()


class ParseGrade(IntEnum):
    """Уровень разбора вложения; OCR выше текстового слоя и не откатывается."""

    TEXT = 0
    OCR = 1


class ConfluenceMarks:
    """Отпечатки версий для реестра: что известно из списка без скачивания.

    Создаётся сборщиком запросов на его раскладку страницы; отпечаток страницы
    несёт версию и раскладку, вложения — версию, дату, размер и тип.
    """

    PAGE_LAYOUT: ClassVar[int] = 2
    """Версия раскладки страницы на секции. Входит в отпечаток: без неё уже
    проиндексированные страницы не переразбираются после смены разбора,
    сколько бы он ни улучшился. Поднимается при каждой такой смене."""

    def __init__(self, page_layout: int = PAGE_LAYOUT) -> None:
        self._page_layout = page_layout

    def page(self, version: int) -> SourceMark:
        return SourceMark(fingerprint=f"v{version}:l{self._page_layout}")

    def attachment(
        self,
        att: AttachmentInfo,
        *,
        parent: SourceId,
        grade: ParseGrade,
        skip: str = "",
    ) -> SourceMark:
        fingerprint = f"v{att.version}:{att.when}:{att.file_size}:{att.media_type}"
        return SourceMark(
            fingerprint=fingerprint,
            grade=int(grade),
            parent=parent,
            skip=skip,
        )


class ConfluenceSourceIds:
    """Identity источников: URL запрошенного объекта без query и фрагмента.

    Страница — её REST-адрес content/{id}, вложение — путь download; один
    объект во всех версиях имеет один id, версия у Confluence живёт в query.
    Создаётся транспортом и обходом источников у себя в конструкторе.
    """

    PAGE_RE: ClassVar[re.Pattern[str]] = re.compile(r"/rest/api/content/([^/?#]+)$")
    ATTACHMENT_MARK: ClassVar[str] = "/download/attachments/"

    def of(self, connection: HttpConnection, path: str) -> SourceId:
        return self.of_url(str(connection.url_of(path)))

    def of_url(self, url: str) -> SourceId:
        """URL без query, фрагмента и учётных данных адреса."""
        bare = httpx.URL(url).copy_with(userinfo=b"", query=None, fragment=None)
        return SourceId(str(bare))

    def page_id_of(self, source_id: SourceId) -> str | None:
        """id страницы из её source_id; None, если это не страница."""
        match = self.PAGE_RE.search(str(source_id))
        if match is None:
            return None

        return match.group(1)

    def is_attachment(self, source_id: SourceId) -> bool:
        return self.ATTACHMENT_MARK in str(source_id)

    def page_ids_of(self, source_ids: Iterable[SourceId]) -> Sequence[str]:
        ids: list[str] = []
        for source_id in source_ids:
            page_id = self.page_id_of(source_id)
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


class TitlesCodec:
    """Кортеж строк в JSON-список и обратно: значение ключей metadata со
    списками заголовков и меток."""

    def decode(self, raw: str) -> tuple[str, ...]:
        titles: list[str] = []
        for item in json.loads(raw):
            titles.append(str(item))

        return tuple(titles)

    def encode(self, value: tuple[str, ...]) -> str:
        return json.dumps(list(value), ensure_ascii=False)


class ConfluenceKeys:
    """Confluence-специфичные ключи metadata: реестр констант, значения
    списков кодирует TitlesCodec, вложение — JSON своей модели."""

    TITLES: ClassVar[TitlesCodec] = TitlesCodec()

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
        decode=TITLES.decode,
        encode=TITLES.encode,
    )
    LABELS: ClassVar[MetadataKey[tuple[str, ...]]] = MetadataKey(
        name="confluence.labels",
        decode=TITLES.decode,
        encode=TITLES.encode,
    )
    """Метки страницы из metadata.labels — настоящие теги Confluence."""

    LINKS: ClassVar[MetadataKey[tuple[str, ...]]] = MetadataKey(
        name="confluence.links",
        decode=TITLES.decode,
        encode=TITLES.encode,
    )
    """Заголовки страниц, на которые ссылается эта: явный граф переходов."""
    ATTACHMENT_INFO: ClassVar[MetadataKey[AttachmentInfo]] = MetadataKey(
        name="confluence.attachment_info",
        decode=AttachmentInfo.model_validate_json,
        encode=AttachmentInfo.model_dump_json,
    )
