"""boba.confluence — формат Confluence: REST-DTO, ключи metadata, разбор JSON.

Пакет держит контракт формата, общий для писателя и читателя индекса:
инструменты `boba.tool.confluence` пишут чанки страниц и вложений с этими
ключами, kb-поиск (`boba.tool.kb`) по ним же собирает колонки выдачи, а
индексатор ix читает Confluence теми же адресами и пагинатором.

Содержимое:

- models.py  — DTO ответов content/search, content/{id} и space; вложение
  (AttachmentInfo) с фильтром и гейтом администратора; ConfluenceSourceId —
  identity страницы и вложения по URL; ConfluenceKeys/HttpKeys — типизированные
  MetadataKey; ConfluenceMarks — отпечатки версий для реестра источников;
  PageSections — результат разбора страницы (карточка, текст, таблицы) с
  порогами раскладки TableShape.
- parsing.py — ConfluenceJson и ConfluenceJsonDecoder: REST-JSON -> RawDocument
  с расширенной metadata; BodyDigest — хэш тела страницы.
- rest.py    — ConfluenceConnection (endpoint: профиль, формат тела, дамп),
  ConfluenceUrl и ConfluenceRest (адреса REST), ConfluencePaginator
  (пагинированные запросы поверх HttpTransport проекта).
- html.py    — ConfluenceHtml и PageOps: разбор HTML Confluence, секции,
  ссылки и конверсия в markdown; закрыт extra `html` (bs4, markdownify)
  и импортируется только там, где он нужен.

Ошибки:
ConfluencePayloadError — REST-ответ не разобран: не тот тип, нет полей схемы.
"""

from __future__ import annotations

from boba.confluence.models import (
    AttachmentBlock,
    AttachmentFilter,
    AttachmentGate,
    AttachmentInfo,
    AttachmentVerdict,
    ConfluenceContent,
    ConfluenceDescription,
    ConfluenceKeys,
    ConfluenceMarks,
    ConfluencePageItem,
    ConfluencePayloadError,
    ConfluencePlainText,
    ConfluenceSourceId,
    ConfluenceSpaceItem,
    HttpKeys,
    PageCardSection,
    PageOutlineItem,
    PageParseRequest,
    PageSection,
    PageSectionKind,
    PageSections,
    PageTableSection,
    PageTextSection,
    ParseGrade,
    TableShape,
)
from boba.confluence.parsing import BodyDigest, ConfluenceJson, ConfluenceJsonDecoder

__all__ = [
    "AttachmentBlock",
    "AttachmentFilter",
    "AttachmentGate",
    "AttachmentInfo",
    "AttachmentVerdict",
    "BodyDigest",
    "ConfluenceContent",
    "ConfluenceDescription",
    "ConfluenceJson",
    "ConfluenceJsonDecoder",
    "ConfluenceKeys",
    "ConfluenceMarks",
    "ConfluencePageItem",
    "ConfluencePayloadError",
    "ConfluencePlainText",
    "ConfluenceSourceId",
    "ConfluenceSpaceItem",
    "HttpKeys",
    "PageCardSection",
    "PageOutlineItem",
    "PageParseRequest",
    "PageSection",
    "PageSectionKind",
    "PageSections",
    "PageTableSection",
    "PageTextSection",
    "ParseGrade",
    "TableShape",
]
