"""Разбор HTML для тел инструментов: недоверенную разметку разбирают только здесь.

Модуль импортируется телами инструментов, работающими в песочнице; в процесс
приложения bs4/markdownify не попадают.

Ошибки: ожидаемых нет. bs4 и markdownify не отказывают на битой разметке —
они её восстанавливают, поэтому любая ошибка здесь означает дефект кода.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any, ClassVar
from urllib.parse import unquote

import markdownify
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from boba.confluence.models import (
    PageCardSection,
    PageOutlineItem,
    PageParseRequest,
    PageSection,
    PageSections,
    PageTableSection,
    PageTextSection,
    TableShape,
)


class HtmlTag(StrEnum):
    """Теги, которые разбирает Confluence-парсер сверх заголовков."""

    TABLE = "table"
    ROW = "tr"
    HEADER_CELL = "th"
    DATA_CELL = "td"
    CAPTION = "caption"
    ANCHOR = "a"
    PAGE_REF = "ri:page"


class HtmlAttr(StrEnum):
    """Атрибуты, из которых берутся ссылки на другие страницы."""

    HREF = "href"
    CONTENT_TITLE = "ri:content-title"


class PageLinkMark(StrEnum):
    """Признаки того, что href ведёт на страницу Confluence, а не наружу."""

    DISPLAY = "/display/"
    VIEWPAGE = "/pages/viewpage.action"


class ConfluenceHtml:
    """Confluence-aware разбор через BeautifulSoup: ac:/ri: остальные не берут."""

    HEADING_TAGS: ClassVar[tuple[str, ...]] = ("h1", "h2", "h3", "h4", "h5", "h6")
    HEADING_TAG_NAMES: ClassVar[frozenset[str]] = frozenset(HEADING_TAGS)

    GOBACK: ClassVar[str] = "_GoBack"
    """Служебный anchor Confluence-экспорта, игнорируется при extraction'е."""

    @staticmethod
    def parse_html(data: str) -> BeautifulSoup:
        return BeautifulSoup(data, "lxml")

    @classmethod
    def collect_headings(cls, soup: BeautifulSoup) -> list[dict[str, Any]]:
        """Заголовки страницы: index 1-based, anchor или пустая строка."""
        headings: list[dict[str, Any]] = []
        for i, tag in enumerate(soup.find_all(list(cls.HEADING_TAGS)), start=1):
            anchor = cls.heading_anchor(tag)
            headings.append(
                {
                    "index": i,
                    "level": int(tag.name[1]),
                    "text": cls.heading_text(tag),
                    "anchor": anchor,
                    "tag": tag,
                }
            )
        return headings

    @classmethod
    def text_between(cls, start_tag: Tag, end_tag: Tag | None) -> str:
        """Текст секции без таблиц: они уходят отдельными записями разбора."""
        parts: list[str] = []
        for el in start_tag.next_elements:
            if el is end_tag:
                break
            if not isinstance(el, NavigableString):
                continue
            if cls.is_inside_heading(el):
                continue
            if cls.is_inside_macro(el):
                continue
            if cls.is_inside_table(el):
                continue
            parts.append(str(el))
        return " ".join(" ".join(parts).split())

    @classmethod
    def collect_tables(cls, soup: BeautifulSoup) -> list[Tag]:
        """Таблицы верхнего уровня в порядке документа; вложенные пропущены.

        Вложенная таблица в Confluence — это вёрстка (макет, панель), а не
        данные: разбирать её как самостоятельную таблицу бессмысленно, её
        текст уже попал в ячейку внешней.
        """
        tables: list[Tag] = []
        for node in soup.find_all(str(HtmlTag.TABLE)):
            if not isinstance(node, Tag):
                continue

            if cls.owner_table(node) is not None:
                continue

            tables.append(node)

        return tables

    @classmethod
    def table_caption(cls, table: Tag) -> str:
        caption = table.find(str(HtmlTag.CAPTION))
        if not isinstance(caption, Tag):
            return ""

        return cls.plain_text(caption)

    @classmethod
    def table_grid(cls, table: Tag) -> list[list[str]]:
        """Строки таблицы как списки текстов ячеек; пустые строки отброшены."""
        grid: list[list[str]] = []
        for row in table.find_all(str(HtmlTag.ROW)):
            if not isinstance(row, Tag):
                continue

            if cls.owner_table(row) is not table:
                continue

            cells = cls.row_cells(row)
            if not cls._has_text(cells):
                continue

            grid.append(cells)

        return grid

    @classmethod
    def row_cells(cls, row: Tag) -> list[str]:
        cells: list[str] = []
        for cell in row.find_all([str(HtmlTag.HEADER_CELL), str(HtmlTag.DATA_CELL)]):
            if not isinstance(cell, Tag):
                continue

            if cell.find_parent(str(HtmlTag.ROW)) is not row:
                continue

            cells.append(cls.plain_text(cell))

        return cells

    @classmethod
    def is_header_row(cls, table: Tag) -> bool:
        """Первая строка — шапка, если в ней есть хотя бы одна ячейка `th`."""
        for row in table.find_all(str(HtmlTag.ROW)):
            if not isinstance(row, Tag):
                continue

            if cls.owner_table(row) is not table:
                continue

            header = row.find(str(HtmlTag.HEADER_CELL))
            return isinstance(header, Tag)

        return False

    @classmethod
    def collect_links(cls, soup: BeautifulSoup) -> tuple[str, ...]:
        """Заголовки страниц, на которые ссылается эта; повторы отброшены.

        Storage-формат даёт ссылку макросом `ri:page`, view-формат — обычным
        `a href`, поэтому берутся оба источника.
        """
        titles: list[str] = []
        for node in soup.find_all(str(HtmlTag.PAGE_REF)):
            if not isinstance(node, Tag):
                continue

            cls._append_unique(titles, cls._attr(node, HtmlAttr.CONTENT_TITLE))

        for node in soup.find_all(str(HtmlTag.ANCHOR)):
            if not isinstance(node, Tag):
                continue

            cls._append_unique(titles, cls._page_link_title(node))

        return tuple(titles)

    @classmethod
    def _page_link_title(cls, node: Tag) -> str:
        href = cls._attr(node, HtmlAttr.HREF)
        if not href:
            return ""

        if not cls._is_page_href(href):
            return ""

        text = cls.plain_text(node)
        if text:
            return text

        return cls._title_from_href(href)

    @staticmethod
    def _is_page_href(href: str) -> bool:
        for mark in PageLinkMark:  # noqa: SIM110
            if mark in href:
                return True

        return False

    @staticmethod
    def _title_from_href(href: str) -> str:
        tail = href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        return unquote(tail).replace("+", " ").strip()

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

    @staticmethod
    def owner_table(node: Tag) -> Tag | None:
        """Ближайшая таблица-владелец узла или None, если узел вне таблиц."""
        parent = node.find_parent(str(HtmlTag.TABLE))
        if isinstance(parent, Tag):
            return parent

        return None

    @classmethod
    def plain_text(cls, node: Tag) -> str:
        parts: list[str] = []
        for el in node.descendants:
            if not isinstance(el, NavigableString):
                continue
            if cls.is_inside_macro(el):
                continue
            parts.append(str(el))
        return " ".join(" ".join(parts).split())

    @classmethod
    def body_text(cls, node: Tag) -> str:
        """Текст узла без содержимого таблиц — они разбираются отдельно."""
        parts: list[str] = []
        for el in node.descendants:
            if not isinstance(el, NavigableString):
                continue

            if cls.is_inside_macro(el):
                continue

            if cls.is_inside_table(el):
                continue

            parts.append(str(el))

        return " ".join(" ".join(parts).split())

    @staticmethod
    def is_confluence_macro(tag: object) -> bool:
        name = getattr(tag, "name", None)
        return bool(name and name.startswith(("ac:", "ri:")))

    @classmethod
    def heading_anchor(cls, tag: Tag) -> str:
        for sm in tag.find_all(
            lambda t: t.name == "ac:structured-macro" and t.get("ac:name") == "anchor",
        ):
            param = sm.find("ac:parameter")
            if param is None:
                continue
            name = param.get_text(strip=True)
            if name and name != cls.GOBACK:
                return name
        return cls.html_id(tag)

    @classmethod
    def heading_text(cls, tag: Tag) -> str:
        parts: list[str] = []
        for el in tag.descendants:
            if not isinstance(el, NavigableString):
                continue
            if cls.is_inside_macro(el):
                continue
            parts.append(str(el))
        return " ".join(" ".join(parts).split())

    @staticmethod
    def html_id(tag: Tag) -> str:
        raw = tag.get("id")
        if isinstance(raw, list):
            if raw:
                return str(raw[0])
            return ""
        if raw:
            return str(raw)
        return ""

    @classmethod
    def is_inside_heading(cls, el: NavigableString) -> bool:
        for parent in el.parents:
            if isinstance(parent, Tag) and parent.name in cls.HEADING_TAG_NAMES:
                return True
        return False

    @classmethod
    def is_inside_macro(cls, el: NavigableString) -> bool:
        for parent in el.parents:
            if isinstance(parent, Tag) and cls.is_confluence_macro(parent):
                return True
        return False

    @staticmethod
    def is_inside_table(el: NavigableString) -> bool:
        for parent in el.parents:
            if isinstance(parent, Tag) and parent.name == HtmlTag.TABLE:
                return True
        return False


class PageOps:
    """Операции над HTML; вызываются телами инструментов напрямую."""

    BREADCRUMB_SEPARATOR: ClassVar[str] = " › "
    TITLE_LEVEL: ClassVar[int] = 0
    BLOCK_TAGS: ClassVar[tuple[str, ...]] = (
        *ConfluenceHtml.HEADING_TAGS,
        HtmlTag.TABLE,
    )
    """Теги, задающие раскладку страницы: заголовок открывает секцию,
    таблица идёт отдельной записью."""

    @staticmethod
    def to_markdown(request: dict[str, Any]) -> dict[str, Any]:
        markdown = markdownify.markdownify(
            request["html"], heading_style=request["heading_style"]
        )
        return {"markdown": markdown}

    @staticmethod
    def plain_text(request: dict[str, Any]) -> dict[str, Any]:
        soup = ConfluenceHtml.parse_html(request["html"])
        return {"text": ConfluenceHtml.plain_text(soup)}

    @classmethod
    def confluence_sections(cls, request: Mapping[str, Any]) -> dict[str, Any]:
        """Разбор страницы: карточка, текст по заголовкам и таблицы отдельно.

        Наружу уходит wire-формат PageSections — ридер конвейера валидирует
        его обратно в модели.
        """
        parsed = PageParseRequest.model_validate(request)
        if not parsed.html.strip():
            return PageSections().model_dump(mode="json")

        soup = ConfluenceHtml.parse_html(parsed.html)
        sections = tuple(cls._sections(soup, parsed))
        return PageSections(sections=sections).model_dump(mode="json")

    @classmethod
    def _sections(
        cls,
        soup: BeautifulSoup,
        parsed: PageParseRequest,
    ) -> Iterator[PageSection]:
        headings = cls._headings(soup)
        links = ConfluenceHtml.collect_links(soup)
        card = cls._card(parsed.title, headings, links)
        if card is not None:
            yield card

        stack: list[tuple[int, str]] = []
        if parsed.title:
            stack.append((cls.TITLE_LEVEL, parsed.title))

        if not headings:
            yield from cls._headless(soup, parsed, stack)
            return

        yield from cls._by_headings(soup, parsed, stack, headings)

    @classmethod
    def _by_headings(
        cls,
        soup: BeautifulSoup,
        parsed: PageParseRequest,
        stack: list[tuple[int, str]],
        headings: list[dict[str, Any]],
    ) -> Iterator[PageSection]:
        """Обход в порядке документа: заголовок открывает секцию, таблица
        идёт своей записью под текущим заголовком."""
        by_tag: dict[int, int] = {}
        for position, heading in enumerate(headings):
            by_tag[id(heading["tag"])] = position

        order = 1
        anchor = ""
        for node in cls._blocks(soup, headings):
            if node.name == HtmlTag.TABLE:
                table = cls._table(node, parsed.table_shape, order, cls._path(stack))
                if table is not None:
                    yield table.model_copy(update={"anchor": anchor})
                    order += 1

                continue

            position = by_tag[id(node)]
            heading = headings[position]
            cls._push(stack, heading["level"], heading["text"])
            anchor = cls._anchor_of(heading)
            yield cls._text(heading, headings, position, order, cls._path(stack))
            order += 1

    @classmethod
    def _headless(
        cls,
        soup: BeautifulSoup,
        parsed: PageParseRequest,
        stack: list[tuple[int, str]],
    ) -> Iterator[PageSection]:
        """Страница без заголовков: текст одной секцией, таблицы — своими."""
        body = soup.body or soup
        text = ConfluenceHtml.body_text(body)
        path = cls._path(stack)
        order = 1
        if text or parsed.title:
            content = text
            if parsed.title:
                content = f"{parsed.title}\n\n{text}".strip()

            yield PageTextSection(
                order=order,
                content=content,
                heading_level=cls.TITLE_LEVEL,
                heading_text=parsed.title,
                heading_path=path,
            )
            order += 1

        for node in ConfluenceHtml.collect_tables(soup):
            table = cls._table(node, parsed.table_shape, order, path)
            if table is None:
                continue

            yield table
            order += 1

    @classmethod
    def _text(
        cls,
        heading: dict[str, Any],
        headings: list[dict[str, Any]],
        position: int,
        order: int,
        path: str,
    ) -> PageTextSection:
        next_tag = None
        if position + 1 < len(headings):
            next_tag = headings[position + 1]["tag"]

        between = ConfluenceHtml.text_between(heading["tag"], next_tag)
        text = heading["text"]
        if between:
            text = f"{text}\n\n{between}"

        return PageTextSection(
            order=order,
            content=text.strip(),
            heading_level=heading["level"],
            heading_text=heading["text"],
            heading_path=path,
            anchor=cls._anchor_of(heading),
        )

    @classmethod
    def _table(
        cls,
        node: Tag,
        shape: TableShape,
        order: int,
        path: str,
    ) -> PageTableSection | None:
        """Таблица в шапку и строки; таблица без данных пропускается."""
        grid = ConfluenceHtml.table_grid(node)
        if not grid:
            return None

        columns: list[str] = []
        body = grid
        if ConfluenceHtml.is_header_row(node):
            columns = grid[0]
            body = grid[1:]

        if not body:
            return None

        width = cls._width(columns, body)
        rows: list[tuple[str, ...]] = []
        for row in body:
            rows.append(cls._padded(row, width))

        layout = shape.layout_for(columns=len(columns), rows=len(rows))
        return PageTableSection(
            order=order,
            heading_path=path,
            caption=ConfluenceHtml.table_caption(node),
            columns=cls._padded(columns, width),
            rows=tuple(rows),
            layout=layout,
        )

    @classmethod
    def _card(
        cls,
        title: str,
        headings: list[dict[str, Any]],
        links: tuple[str, ...],
    ) -> PageCardSection | None:
        """Карточка страницы; пустую (ни заголовка, ни разделов) не выпускаем."""
        outline: list[PageOutlineItem] = []
        for heading in headings:
            outline.append(
                PageOutlineItem(
                    level=heading["level"],
                    text=heading["text"],
                    anchor=heading["anchor"],
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

    @classmethod
    def _blocks(
        cls,
        soup: BeautifulSoup,
        headings: list[dict[str, Any]],
    ) -> list[Tag]:
        """Заголовки и таблицы верхнего уровня в порядке документа."""
        keep: set[int] = set()
        for heading in headings:
            keep.add(id(heading["tag"]))

        names = list(cls.BLOCK_TAGS)
        blocks: list[Tag] = []
        for node in soup.find_all(names):
            if not isinstance(node, Tag):
                continue

            if node.name == HtmlTag.TABLE:
                if ConfluenceHtml.owner_table(node) is None:
                    blocks.append(node)

                continue

            if id(node) in keep:
                blocks.append(node)

        return blocks

    @staticmethod
    def _headings(soup: BeautifulSoup) -> list[dict[str, Any]]:
        headings: list[dict[str, Any]] = []
        for heading in ConfluenceHtml.collect_headings(soup):
            if heading["text"].strip():
                headings.append(heading)

        return headings

    @staticmethod
    def _anchor_of(heading: dict[str, Any]) -> str:
        anchor = heading["anchor"]
        if anchor:
            return str(anchor)

        return f"idx:{heading['index']}"

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

    @classmethod
    def _path(cls, stack: list[tuple[int, str]]) -> str:
        parts: list[str] = []
        for _, text in stack:
            parts.append(text)
        return cls.BREADCRUMB_SEPARATOR.join(parts)
