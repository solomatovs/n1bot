"""Разбор HTML Confluence и конверсия в markdown: недоверенную разметку
разбирают только здесь.

Модуль закрыт extra `html` пакета (bs4, markdownify): тела инструментов
импортируют его в песочнице, индексатор — у себя в процессе; приложение
чата его не импортирует.

Ошибки: ожидаемых нет. bs4 и markdownify не отказывают на битой разметке —
они её восстанавливают, поэтому любая ошибка здесь означает дефект кода.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from markdownify import MarkdownConverter
from pydantic import BaseModel, ConfigDict

from boba.confluence.models import (
    LinkKind,
    PageCardSection,
    PageHref,
    PageLink,
    PageOutlineItem,
    PageParseRequest,
    PageSection,
    PageSections,
    PageTableSection,
    PageTarget,
    PageTextSection,
    TableShape,
)

__all__ = [
    "ConfluencePage",
    "MarkdownRender",
    "MarkdownRequest",
    "PageHeading",
    "PageLayout",
    "PageMarkdown",
    "PlainTextRender",
    "PlainTextRequest",
    "SectionsRender",
]


class HtmlTag(StrEnum):
    """Теги, которые разбирает Confluence-парсер сверх заголовков."""

    TABLE = "table"
    ROW = "tr"
    HEADER_CELL = "th"
    DATA_CELL = "td"
    CAPTION = "caption"
    ANCHOR = "a"
    PAGE_REF = "ri:page"
    STRUCTURED_MACRO = "ac:structured-macro"
    PARAMETER = "ac:parameter"


class HtmlAttr(StrEnum):
    """Атрибуты, из которых берутся ссылки на другие страницы и якоря."""

    HREF = "href"
    CONTENT_TITLE = "ri:content-title"
    MACRO_NAME = "ac:name"
    ID = "id"


@dataclass(frozen=True)
class PageHeading:
    """Заголовок страницы: порядковый номер с единицы, уровень, текст, якорь
    и сам узел дерева для обхода текста между заголовками."""

    index: int
    level: int
    text: str
    anchor: str
    tag: Tag

    def anchor_or_index(self) -> str:
        """Якорь ссылки на секцию; без явного якоря — порядковый номер."""
        if self.anchor:
            return self.anchor

        return f"idx:{self.index}"


class ConfluencePage:
    """Одна страница Confluence, разобранная BeautifulSoup один раз.

    Создаётся из HTML тела и знания о самой странице (id и заголовок), чтобы
    ссылки на себя не считались переходами. Дальше отдаёт заголовки, таблицы,
    ссылки и текст узлов; макросы ac:/ri: в текст не попадают. Владелец
    (PageLayout, PageMarkdown, разбор индексатора) закрывает страницу через
    close(), когда дерево больше не нужно.
    """

    PARSER: ClassVar[str] = "lxml"
    HEADING_TAGS: ClassVar[tuple[str, ...]] = ("h1", "h2", "h3", "h4", "h5", "h6")
    HEADING_TAG_NAMES: ClassVar[frozenset[str]] = frozenset(HEADING_TAGS)
    MACRO_PREFIXES: ClassVar[tuple[str, ...]] = ("ac:", "ri:")
    ANCHOR_MACRO: ClassVar[str] = "anchor"

    GOBACK: ClassVar[str] = "_GoBack"
    """Служебный anchor Confluence-экспорта, игнорируется при extraction'е."""

    def __init__(self, html: str, *, page_id: str = "", title: str = "") -> None:
        self._soup = BeautifulSoup(html, self.PARSER)
        self._own = PageTarget(page_id=page_id, title=title)

    @property
    def soup(self) -> BeautifulSoup:
        return self._soup

    @property
    def own(self) -> PageTarget:
        return self._own

    def close(self) -> None:
        self._soup.decompose()

    def body(self) -> Tag:
        """Узел тела; у фрагмента без <body> — корень дерева."""
        found = self._soup.body
        if found is None:
            return self._soup

        return found

    def headings(self) -> tuple[PageHeading, ...]:
        """Заголовки страницы в порядке документа, пустые пропущены."""
        found: list[PageHeading] = []
        index = 0
        for tag in self._soup.find_all(list(self.HEADING_TAGS)):
            if not isinstance(tag, Tag):
                continue

            index += 1
            text = self.heading_text(tag)
            if not text.strip():
                continue

            found.append(
                PageHeading(
                    index=index,
                    level=int(tag.name[1]),
                    text=text,
                    anchor=self.heading_anchor(tag),
                    tag=tag,
                )
            )

        return tuple(found)

    def text_between(self, start_tag: Tag, end_tag: Tag | None) -> str:
        """Текст секции без таблиц: они уходят отдельными записями разбора."""
        parts: list[str] = []
        for el in start_tag.next_elements:
            if el is end_tag:
                break

            if not isinstance(el, NavigableString):
                continue

            if self.is_inside_heading(el):
                continue

            if self.is_inside_macro(el):
                continue

            if self.is_inside_table(el):
                continue

            parts.append(str(el))

        return " ".join(" ".join(parts).split())

    def tables(self) -> tuple[Tag, ...]:
        """Таблицы верхнего уровня в порядке документа; вложенные пропущены.

        Вложенная таблица в Confluence — это вёрстка (макет, панель), а не
        данные: разбирать её как самостоятельную таблицу бессмысленно, её
        текст уже попал в ячейку внешней.
        """
        found: list[Tag] = []
        for node in self._soup.find_all(str(HtmlTag.TABLE)):
            if not isinstance(node, Tag):
                continue

            if self.owner_table(node) is not None:
                continue

            found.append(node)

        return tuple(found)

    def blocks(self, headings: tuple[PageHeading, ...]) -> tuple[Tag, ...]:
        """Заголовки и таблицы верхнего уровня в порядке документа."""
        keep: set[int] = set()
        for heading in headings:
            keep.add(id(heading.tag))

        names = [*self.HEADING_TAGS, str(HtmlTag.TABLE)]
        found: list[Tag] = []
        for node in self._soup.find_all(names):
            if not isinstance(node, Tag):
                continue

            if node.name == HtmlTag.TABLE:
                if self.owner_table(node) is None:
                    found.append(node)

                continue

            if id(node) in keep:
                found.append(node)

        return tuple(found)

    def table_caption(self, table: Tag) -> str:
        caption = table.find(str(HtmlTag.CAPTION))
        if not isinstance(caption, Tag):
            return ""

        return self.plain_text(caption)

    def table_grid(self, table: Tag) -> list[list[str]]:
        """Строки таблицы как списки текстов ячеек; пустые строки отброшены."""
        grid: list[list[str]] = []
        for row in table.find_all(str(HtmlTag.ROW)):
            if not isinstance(row, Tag):
                continue

            if self.owner_table(row) is not table:
                continue

            cells = self.row_cells(row)
            if not self._has_text(cells):
                continue

            grid.append(cells)

        return grid

    def row_cells(self, row: Tag) -> list[str]:
        cells: list[str] = []
        names = [str(HtmlTag.HEADER_CELL), str(HtmlTag.DATA_CELL)]
        for cell in row.find_all(names):
            if not isinstance(cell, Tag):
                continue

            if cell.find_parent(str(HtmlTag.ROW)) is not row:
                continue

            cells.append(self.plain_text(cell))

        return cells

    def is_header_row(self, table: Tag) -> bool:
        """Первая строка — шапка, если в ней есть хотя бы одна ячейка `th`."""
        for row in table.find_all(str(HtmlTag.ROW)):
            if not isinstance(row, Tag):
                continue

            if self.owner_table(row) is not table:
                continue

            header = row.find(str(HtmlTag.HEADER_CELL))
            return isinstance(header, Tag)

        return False

    def links(self) -> tuple[str, ...]:
        """Заголовки других страниц, на которые ссылается эта.

        Storage-формат даёт ссылку макросом `ri:page`, view-формат — `a href`
        в одной из форм PageHref. Повторы и ссылки на саму страницу (её id,
        заголовок, фрагменты) отброшены.
        """
        titles: list[str] = []
        for node in self._soup.find_all(str(HtmlTag.PAGE_REF)):
            if not isinstance(node, Tag):
                continue

            target = self._attr(node, HtmlAttr.CONTENT_TITLE)
            if target == self._own.title:
                continue

            self._append_unique(titles, target)

        for node in self._soup.find_all(str(HtmlTag.ANCHOR)):
            if not isinstance(node, Tag):
                continue

            self._append_unique(titles, self._link_label(node))

        return tuple(titles)

    def targets(self) -> tuple[PageLink, ...]:
        """Цели ссылок на другие страницы с видом записи — для рёбер графа.

        Повторы, ссылки на саму страницу и короткие /x/<код> без id и заголовка
        отброшены.
        """
        found: list[PageLink] = []
        seen: set[tuple[str, str]] = set()

        for node in self._soup.find_all(str(HtmlTag.PAGE_REF)):
            if not isinstance(node, Tag):
                continue

            target = PageTarget(title=self._attr(node, HtmlAttr.CONTENT_TITLE))
            self._append_target(
                found, seen, PageLink(target=target, kind=LinkKind.MACRO)
            )

        for node in self._soup.find_all(str(HtmlTag.ANCHOR)):
            if not isinstance(node, Tag):
                continue

            target = PageHref(self._attr(node, HtmlAttr.HREF)).target()
            if target is None:
                continue

            kind = LinkKind.TITLE
            if target.page_id:
                kind = LinkKind.ID

            self._append_target(found, seen, PageLink(target=target, kind=kind))

        return tuple(found)

    def owner_table(self, node: Tag) -> Tag | None:
        """Ближайшая таблица-владелец узла или None, если узел вне таблиц."""
        parent = node.find_parent(str(HtmlTag.TABLE))
        if isinstance(parent, Tag):
            return parent

        return None

    def plain_text(self, node: Tag) -> str:
        parts: list[str] = []
        for el in node.descendants:
            if not isinstance(el, NavigableString):
                continue

            if self.is_inside_macro(el):
                continue

            parts.append(str(el))

        return " ".join(" ".join(parts).split())

    def body_text(self, node: Tag) -> str:
        """Текст узла без содержимого таблиц — они разбираются отдельно."""
        parts: list[str] = []
        for el in node.descendants:
            if not isinstance(el, NavigableString):
                continue

            if self.is_inside_macro(el):
                continue

            if self.is_inside_table(el):
                continue

            parts.append(str(el))

        return " ".join(" ".join(parts).split())

    def heading_anchor(self, tag: Tag) -> str:
        for macro in tag.find_all(str(HtmlTag.STRUCTURED_MACRO)):
            if not isinstance(macro, Tag):
                continue

            if self._attr(macro, HtmlAttr.MACRO_NAME) != self.ANCHOR_MACRO:
                continue

            param = macro.find(str(HtmlTag.PARAMETER))
            if not isinstance(param, Tag):
                continue

            name = param.get_text(strip=True)
            if name and name != self.GOBACK:
                return name

        return self._attr(tag, HtmlAttr.ID)

    def heading_text(self, tag: Tag) -> str:
        return self.plain_text(tag)

    def is_macro(self, tag: Tag) -> bool:
        name = tag.name
        if not name:
            return False

        return name.startswith(self.MACRO_PREFIXES)

    def is_inside_heading(self, el: NavigableString) -> bool:
        for parent in el.parents:
            if isinstance(parent, Tag) and parent.name in self.HEADING_TAG_NAMES:
                return True

        return False

    def is_inside_macro(self, el: NavigableString) -> bool:
        for parent in el.parents:
            if isinstance(parent, Tag) and self.is_macro(parent):
                return True

        return False

    def is_inside_table(self, el: NavigableString) -> bool:
        for parent in el.parents:
            if isinstance(parent, Tag) and parent.name == HtmlTag.TABLE:
                return True

        return False

    def _append_target(
        self, links: list[PageLink], seen: set[tuple[str, str]], link: PageLink
    ) -> None:
        target = link.target
        if not target.page_id and not target.title:
            return

        if target.is_page(page_id=self._own.page_id, title=self._own.title):
            return

        key = (target.page_id, target.title)
        if key in seen:
            return

        seen.add(key)
        links.append(link)

    def _link_label(self, node: Tag) -> str:
        """Подпись ссылки на другую страницу или пустая строка."""
        target = PageHref(self._attr(node, HtmlAttr.HREF)).target()
        if target is None:
            return ""

        if target.is_page(page_id=self._own.page_id, title=self._own.title):
            return ""

        text = self.plain_text(node)
        if text:
            return text

        return target.title

    @staticmethod
    def _attr(node: Tag, name: HtmlAttr) -> str:
        raw = node.get(str(name))
        if isinstance(raw, list):
            if not raw:
                return ""

            return str(raw[0]).strip()

        if raw is None:
            return ""

        return str(raw).strip()

    @staticmethod
    def _append_unique(titles: list[str], value: str) -> None:
        if not value:
            return

        if value in titles:
            return

        titles.append(value)

    @staticmethod
    def _has_text(cells: list[str]) -> bool:
        for cell in cells:  # noqa: SIM110
            if cell.strip():
                return True

        return False


class PageMarkdown:
    """Конверсия страницы в markdown с заданным стилем заголовков.

    Экранирование `_` и `*` нужно для чтения человеком; текст индекса идёт без
    него, иначе идентификаторы вида dm.order_lines теряют вид.
    """

    def __init__(self, heading_style: str, *, escape: bool) -> None:
        self._converter = MarkdownConverter(
            heading_style=heading_style,
            escape_underscores=escape,
            escape_asterisks=escape,
        )

    def render(self, page: ConfluencePage) -> str:
        return str(self._converter.convert_soup(page.soup)).strip()


class PageLayout:
    """Раскладка страницы на секции: карточка, текст по заголовкам, таблицы
    отдельными записями под текущим заголовком.

    Создаётся над разобранной страницей и порогами раскладки таблиц; sections()
    отдаёт модель PageSections, которую ридер конвейера кладёт в индекс.
    """

    TITLE_LEVEL: ClassVar[int] = 0

    def __init__(self, page: ConfluencePage, shape: TableShape) -> None:
        self._page = page
        self._shape = shape

    def sections(self) -> PageSections:
        return PageSections(sections=tuple(self._sections()))

    def _sections(self) -> Iterator[PageSection]:
        headings = self._page.headings()
        title = self._page.own.title
        card = self._card(title, headings, self._page.links())
        if card is not None:
            yield card

        stack: list[tuple[int, str]] = []
        if title:
            stack.append((self.TITLE_LEVEL, title))

        if not headings:
            yield from self._headless(stack)
            return

        yield from self._by_headings(stack, headings)

    def _by_headings(
        self, stack: list[tuple[int, str]], headings: tuple[PageHeading, ...]
    ) -> Iterator[PageSection]:
        """Обход в порядке документа: заголовок открывает секцию, таблица
        идёт своей записью под текущим заголовком."""
        by_tag: dict[int, int] = {}
        for position, heading in enumerate(headings):
            by_tag[id(heading.tag)] = position

        order = 1
        anchor = ""
        for node in self._page.blocks(headings):
            if node.name == HtmlTag.TABLE:
                table = self._table(node, order, self._path(stack))
                if table is not None:
                    yield table.model_copy(update={"anchor": anchor})
                    order += 1

                continue

            position = by_tag[id(node)]
            heading = headings[position]
            self._push(stack, heading.level, heading.text)
            anchor = heading.anchor_or_index()
            yield self._text(headings, position, order, self._path(stack))
            order += 1

    def _headless(self, stack: list[tuple[int, str]]) -> Iterator[PageSection]:
        """Страница без заголовков: текст одной секцией, таблицы — своими."""
        title = self._page.own.title
        text = self._page.body_text(self._page.body())
        path = self._path(stack)
        order = 1
        if text or title:
            content = text
            if title:
                content = f"{title}\n\n{text}".strip()

            yield PageTextSection(
                order=order,
                content=content,
                heading_level=self.TITLE_LEVEL,
                heading_text=title,
                heading_path=path,
            )
            order += 1

        for node in self._page.tables():
            table = self._table(node, order, path)
            if table is None:
                continue

            yield table
            order += 1

    def _text(
        self, headings: tuple[PageHeading, ...], position: int, order: int, path: str
    ) -> PageTextSection:
        heading = headings[position]
        next_tag = None
        if position + 1 < len(headings):
            next_tag = headings[position + 1].tag

        between = self._page.text_between(heading.tag, next_tag)
        text = heading.text
        if between:
            text = f"{text}\n\n{between}"

        return PageTextSection(
            order=order,
            content=text.strip(),
            heading_level=heading.level,
            heading_text=heading.text,
            heading_path=path,
            anchor=heading.anchor_or_index(),
        )

    def _table(self, node: Tag, order: int, path: str) -> PageTableSection | None:
        """Таблица в шапку и строки; таблица без данных пропускается."""
        grid = self._page.table_grid(node)
        if not grid:
            return None

        columns: list[str] = []
        body = grid
        if self._page.is_header_row(node):
            columns = grid[0]
            body = grid[1:]

        if not body:
            return None

        width = self._width(columns, body)
        rows: list[tuple[str, ...]] = []
        for row in body:
            rows.append(self._padded(row, width))

        layout = self._shape.layout_for(columns=len(columns), rows=len(rows))
        return PageTableSection(
            order=order,
            heading_path=path,
            caption=self._page.table_caption(node),
            columns=self._padded(columns, width),
            rows=tuple(rows),
            layout=layout,
        )

    def _card(
        self, title: str, headings: tuple[PageHeading, ...], links: tuple[str, ...]
    ) -> PageCardSection | None:
        """Карточка страницы; пустую (ни заголовка, ни разделов) не выпускаем."""
        outline: list[PageOutlineItem] = []
        for heading in headings:
            outline.append(
                PageOutlineItem(
                    level=heading.level, text=heading.text, anchor=heading.anchor
                )
            )

        if not title and not outline and not links:
            return None

        return PageCardSection(
            order=0,
            title=title,
            heading_path=title,
            outline=tuple(outline),
            links=links,
        )

    @staticmethod
    def _width(columns: list[str], body: list[list[str]]) -> int:
        width = len(columns)
        for row in body:
            width = max(width, len(row))

        return width

    @staticmethod
    def _padded(cells: list[str], width: int) -> tuple[str, ...]:
        padded = list(cells)
        while len(padded) < width:
            padded.append("")

        return tuple(padded)

    @staticmethod
    def _push(stack: list[tuple[int, str]], level: int, text: str) -> None:
        while stack and stack[-1][0] >= level:
            stack.pop()

        stack.append((level, text))

    def _path(self, stack: list[tuple[int, str]]) -> str:
        parts: list[str] = []
        for _, text in stack:
            parts.append(text)

        return " › ".join(parts)


class MarkdownRequest(BaseModel):
    """Вход конверсии в markdown: тело, стиль заголовков, экранировать ли
    `_` и `*` (для чтения человеком да, для текста индекса нет)."""

    model_config = ConfigDict(extra="forbid")

    html: str
    heading_style: str
    escape: bool = True


class PlainTextRequest(BaseModel):
    """Вход извлечения плоского текста."""

    model_config = ConfigDict(extra="forbid")

    html: str


class MarkdownRender:
    """Конверсия HTML в markdown для тела инструмента: запрос словарём из
    JSON валидируется в модель, конвертер собирается в конструкторе."""

    def __init__(self, request: Mapping[str, Any]) -> None:
        self._request = MarkdownRequest.model_validate(request)
        self._markdown = PageMarkdown(
            self._request.heading_style, escape=self._request.escape
        )

    def run(self) -> dict[str, Any]:
        page = ConfluencePage(self._request.html)
        try:
            text = self._markdown.render(page)
        finally:
            page.close()

        return {"markdown": text}


class PlainTextRender:
    """Плоский текст страницы для тела инструмента."""

    def __init__(self, request: Mapping[str, Any]) -> None:
        self._request = PlainTextRequest.model_validate(request)

    def run(self) -> dict[str, Any]:
        page = ConfluencePage(self._request.html)
        try:
            text = page.plain_text(page.body())
        finally:
            page.close()

        return {"text": text}


class SectionsRender:
    """Разбор страницы на секции для тела инструмента: карточка, текст по
    заголовкам и таблицы отдельно; наружу wire-формат PageSections, который
    ридер конвейера валидирует обратно в модель."""

    def __init__(self, request: Mapping[str, Any]) -> None:
        self._request = PageParseRequest.model_validate(request)

    def run(self) -> dict[str, Any]:
        request = self._request
        if not request.html.strip():
            return PageSections().model_dump(mode="json")

        page = ConfluencePage(
            request.html, page_id=request.page_id, title=request.title
        )
        try:
            sections = PageLayout(page, request.table_shape).sections()
        finally:
            page.close()

        return sections.model_dump(mode="json")
