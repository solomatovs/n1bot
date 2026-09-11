"""Разбор Confluence-HTML: PageOps как чистые функции + изоляция парсеров.

PageOps больше не payload за протоколом — его зовут тела инструментов
напрямую, поэтому и тесты зовут его напрямую.
"""

from __future__ import annotations

import pytest

from boba.confluence.models import (
    PageCardSection,
    PageHref,
    PageParseRequest,
    PageSections,
    PageTableSection,
    PageTarget,
    PageTextSection,
    TableShape,
)
from boba.indexing import SourceId, TableLayout, TableSection
from boba.tool.confluence.html import PageOps

_HTML = (
    "<html><body><h1>Заголовок</h1><p>Абзац с <b>жирным</b>.</p>"
    '<a href="https://example.com">ссылка</a></body></html>'
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestToMarkdown:
    """Конвертация HTML в Markdown."""

    @staticmethod
    def _run(html: str) -> str:
        answer = PageOps.to_markdown({"html": html, "heading_style": "ATX"})
        return str(answer["markdown"])

    def test_headings_and_emphasis(self) -> None:
        markdown = self._run(_HTML)
        if "# Заголовок" not in markdown:
            raise AssertionError('"# Заголовок" in markdown')
        if "**жирным**" not in markdown:
            raise AssertionError('"**жирным**" in markdown')

    def test_links_are_kept(self) -> None:
        if "[ссылка](https://example.com)" not in self._run(_HTML):
            raise AssertionError('"[ссылка](https://example.com)" in self._run(_HTML)')

    def test_empty_html_is_allowed(self) -> None:
        if self._run("") != "":
            raise AssertionError('self._run("") == ""')


_CONFLUENCE_HTML = (
    "<html><body>"
    '<h1 id="intro">Введение</h1><p>Первый абзац.</p>'
    "<h2>Детали</h2><p>Второй абзац.</p>"
    '<ac:structured-macro ac:name="info">служебное</ac:structured-macro>'
    "</body></html>"
)


_SHAPE = TableShape(row_layout_max_columns=4, row_layout_min_rows=3)

_TABLE_HTML = (
    "<html><body>"
    "<h1>Настройки</h1><p>Вводный абзац.</p>"
    "<table><caption>Таймауты</caption>"
    "<tr><th>Параметр</th><th>Значение</th><th>Описание</th></tr>"
    "<tr><td>connect_timeout</td><td>30s</td><td>установка соединения</td></tr>"
    "<tr><td>read_timeout</td><td>60s</td><td>чтение ответа</td></tr>"
    "<tr><td>retry_attempts</td><td>3</td><td>повторы запроса</td></tr>"
    "</table>"
    "</body></html>"
)


def _parse(html: str, title: str, page_id: str = "42") -> PageSections:
    request = PageParseRequest(
        html=html,
        title=title,
        page_id=page_id,
        table_shape=_SHAPE,
    )
    answer = PageOps.confluence_sections(request.model_dump(mode="json"))
    return PageSections.model_validate(answer)


def _texts(parsed: PageSections) -> list[PageTextSection]:
    rows: list[PageTextSection] = []
    for section in parsed.sections:
        if isinstance(section, PageTextSection):
            rows.append(section)

    return rows


def _tables(parsed: PageSections) -> list[PageTableSection]:
    rows: list[PageTableSection] = []
    for section in parsed.sections:
        if isinstance(section, PageTableSection):
            rows.append(section)

    return rows


def _card(parsed: PageSections) -> PageCardSection:
    for section in parsed.sections:
        if isinstance(section, PageCardSection):
            return section

    raise AssertionError("страница разобрана без карточки")


class TestConfluenceSections:
    """Heading-aware нарезка страницы."""

    def test_section_per_heading(self) -> None:
        sections = _texts(_parse(_CONFLUENCE_HTML, "Страница"))
        if [s.heading_text for s in sections] != ["Введение", "Детали"]:
            raise AssertionError('[s.heading_text for s in sections] == ["Введение…')
        if [s.heading_level for s in sections] != [1, 2]:
            raise AssertionError("[s.heading_level for s in sections] == [1, 2]")

    def test_breadcrumb_starts_from_title(self) -> None:
        sections = _texts(_parse(_CONFLUENCE_HTML, "Страница"))
        if sections[0].heading_path != "Страница › Введение":
            raise AssertionError('sections[0].heading_path == "Страница › Введение"')
        if sections[1].heading_path != "Страница › Введение › Детали":
            raise AssertionError('sections[1].heading_path == "Страница › Введение…')

    def test_text_follows_heading(self) -> None:
        sections = _texts(_parse(_CONFLUENCE_HTML, "Страница"))
        if sections[0].content != "Введение\n\nПервый абзац.":
            raise AssertionError('sections[0].content == "Введение\\n\\nПервый абзац…')

    def test_macros_are_dropped(self) -> None:
        """Содержимое ac:*/ri: в текст не попадает."""
        for section in _texts(_parse(_CONFLUENCE_HTML, "Страница")):
            if "служебное" in section.content:
                raise AssertionError('"служебное" not in section.content')


class TestTables:
    """Таблица доживает до чанка отдельной записью с разобранной шапкой."""

    def test_table_becomes_its_own_section(self) -> None:
        tables = _tables(_parse(_TABLE_HTML, "Страница"))
        if len(tables) != 1:
            raise AssertionError("len(tables) == 1")

        table = tables[0]
        if table.columns != ("Параметр", "Значение", "Описание"):
            raise AssertionError("шапка таблицы разобрана неверно")
        if len(table.rows) != 3:
            raise AssertionError("len(table.rows) == 3")
        if table.caption != "Таймауты":
            raise AssertionError('table.caption == "Таймауты"')

    def test_table_text_is_not_duplicated_in_the_section(self) -> None:
        """Ячейки не должны попасть ещё и в плоский текст секции."""
        for section in _texts(_parse(_TABLE_HTML, "Страница")):
            if "connect_timeout" in section.content:
                raise AssertionError('"connect_timeout" not in section.content')

    def test_narrow_and_long_table_goes_row_by_row(self) -> None:
        table = _tables(_parse(_TABLE_HTML, "Страница"))[0]
        if table.layout is not TableLayout.ROWS:
            raise AssertionError("table.layout is TableLayout.ROWS")

    def test_wide_table_stays_a_grid(self) -> None:
        wide = (
            "<html><body><table>"
            "<tr><th>a</th><th>b</th><th>c</th><th>d</th><th>e</th></tr>"
            "<tr><td>1</td><td>2</td><td>3</td><td>4</td><td>5</td></tr>"
            "<tr><td>6</td><td>7</td><td>8</td><td>9</td><td>0</td></tr>"
            "<tr><td>1</td><td>2</td><td>3</td><td>4</td><td>5</td></tr>"
            "</table></body></html>"
        )
        table = _tables(_parse(wide, "Страница"))[0]
        if table.layout is not TableLayout.GRID:
            raise AssertionError("table.layout is TableLayout.GRID")

    def test_layout_grid_repeats_the_header_in_every_chunk(self) -> None:
        """Ради этого таблица и живёт отдельной секцией."""
        table = _tables(_parse(_TABLE_HTML, "Страница"))[0]
        section = TableSection(
            source_id=SourceId("page"),
            content=table.caption,
            columns=table.columns,
            rows=table.rows,
            layout=TableLayout.GRID,
        )
        plan = section.to_format_plan()
        if "| Параметр | Значение | Описание |" not in plan.repeat_header:
            raise AssertionError("шапка не попала в repeat_header")

    def test_layout_rows_names_every_value(self) -> None:
        table = _tables(_parse(_TABLE_HTML, "Страница"))[0]
        section = TableSection(
            source_id=SourceId("page"),
            content=table.caption,
            columns=table.columns,
            rows=table.rows,
            layout=TableLayout.ROWS,
        )
        plan = section.to_format_plan()
        first = plan.blocks[0].format_content
        if "Параметр: connect_timeout" not in first:
            raise AssertionError('"Параметр: connect_timeout" in first')
        if "Значение: 30s" not in first:
            raise AssertionError('"Значение: 30s" in first')


class TestCard:
    """Карточка страницы: оглавление и ссылки из структуры, без выдумок."""

    def test_outline_repeats_page_headings(self) -> None:
        card = _card(_parse(_CONFLUENCE_HTML, "Страница"))
        if [item.text for item in card.outline] != ["Введение", "Детали"]:
            raise AssertionError('[item.text …] == ["Введение", "Детали"]')
        if [item.level for item in card.outline] != [1, 2]:
            raise AssertionError("[item.level …] == [1, 2]")

    def test_card_is_the_first_section(self) -> None:
        parsed = _parse(_CONFLUENCE_HTML, "Страница")
        if not isinstance(parsed.sections[0], PageCardSection):
            raise AssertionError("карточка идёт первой секцией страницы")

    def test_links_to_other_pages_are_collected(self) -> None:
        html = (
            "<html><body><h1>Обзор</h1>"
            '<p>см. <a href="/display/SPACE/Соседняя+страница">Соседняя</a> '
            'и <a href="https://example.com/external">внешнюю</a></p>'
            "</body></html>"
        )
        card = _card(_parse(html, "Страница"))
        if card.links != ("Соседняя",):
            raise AssertionError('card.links == ("Соседняя",)')

    def test_storage_format_page_refs_are_collected(self) -> None:
        html = (
            "<html><body><h1>Обзор</h1>"
            '<ac:link><ri:page ri:content-title="Другая страница"/></ac:link>'
            "</body></html>"
        )
        card = _card(_parse(html, "Страница"))
        if card.links != ("Другая страница",):
            raise AssertionError('card.links == ("Другая страница",)')


class TestPlainText:
    def test_tags_are_stripped(self) -> None:
        answer = PageOps.plain_text({"html": _HTML})
        text = str(answer["text"])
        if "Заголовок" not in text:
            raise AssertionError('"Заголовок" in text')
        if "<b>" in text:
            raise AssertionError('"<b>" not in text')


class TestPageHref:
    """Формы адресов страниц, встречающиеся на реальном Confluence."""

    def test_spaces_pages_form_carries_id_and_title(self) -> None:
        href = (
            "https://cwiki.apache.org/confluence/spaces/FLINK/pages/199527106/"
            "DRAFT+FLIP-202+Introduce+ClickHouse+Connector"
        )
        expected = PageTarget(
            page_id="199527106",
            title="DRAFT FLIP-202 Introduce ClickHouse Connector",
        )
        if PageHref.parse(href) != expected:
            raise AssertionError(f"{PageHref.parse(href)!r} != {expected!r}")

    def test_spaces_pages_form_without_title(self) -> None:
        target = PageHref.parse("/confluence/spaces/FLINK/pages/199527106")
        if target != PageTarget(page_id="199527106"):
            raise AssertionError(f"{target!r}")

    def test_display_form_carries_decoded_title(self) -> None:
        target = PageHref.parse("/confluence/display/AIRFLOW/AIP-98%3A+Add+async")
        if target != PageTarget(title="AIP-98: Add async"):
            raise AssertionError(f"{target!r}")

    def test_viewpage_form_carries_id(self) -> None:
        href = "/confluence/pages/viewpage.action?pageId=451969699"
        if PageHref.parse(href) != PageTarget(page_id="451969699"):
            raise AssertionError(f"{PageHref.parse(href)!r}")

    def test_tiny_link_is_a_page_without_identity(self) -> None:
        if PageHref.parse("/confluence/x/54EmGQ") != PageTarget():
            raise AssertionError("короткая ссылка /x/ — страница без id и заголовка")

    @pytest.mark.parametrize(
        "href",
        [
            "/confluence/display/~marcus",
            "#Section-anchor",
            "https://meet.google.com/pzy-haeg-auf",
            "/confluence/download/attachments/446071769/readiness.html?version=11",
            "/confluence/pages/resumedraft.action?draftId=152112052",
            "/confluence/pages/viewpage.action",
            "",
        ],
    )
    def test_not_a_page(self, href: str) -> None:
        if PageHref.parse(href) is not None:
            raise AssertionError(f"{href!r} не должен считаться страницей")


class TestLinkCollection:
    """Ссылки карточки: другие страницы, без себя, профилей и внешних адресов."""

    def test_modern_spaces_form_is_collected(self) -> None:
        html = (
            "<html><body><h1>Обзор</h1>"
            '<a href="/confluence/spaces/FLINK/pages/7/FLIP-7">FLIP-7: Scala</a>'
            "</body></html>"
        )
        card = _card(_parse(html, "Страница"))
        if card.links != ("FLIP-7: Scala",):
            raise AssertionError(f"card.links == {card.links!r}")

    def test_links_to_the_page_itself_are_dropped(self) -> None:
        html = (
            "<html><body><h1>Обзор</h1>"
            '<a href="#Obzor-razdel">к разделу</a> '
            '<a href="/confluence/spaces/S/pages/42/Страница">сама по id</a> '
            '<a href="/confluence/display/S/Страница#x">сама по заголовку</a> '
            '<a href="/confluence/pages/viewpage.action?pageId=42">сама старой</a> '
            '<ac:link><ri:page ri:content-title="Страница"/></ac:link>'
            '<a href="/confluence/spaces/S/pages/7/Другая">Другая</a>'
            "</body></html>"
        )
        card = _card(_parse(html, "Страница", page_id="42"))
        if card.links != ("Другая",):
            raise AssertionError(f"card.links == {card.links!r}")

    def test_user_profiles_and_external_links_are_not_pages(self) -> None:
        html = (
            "<html><body><h1>Обзор</h1>"
            '<a href="/confluence/display/~marcus">Marcus</a> '
            '<a href="https://issues.apache.org/jira/browse/FLINK-1">FLINK-1</a> '
            '<a href="/confluence/download/attachments/1/a.pdf">a.pdf</a>'
            "</body></html>"
        )
        card = _card(_parse(html, "Страница"))
        if card.links:
            raise AssertionError(f"card.links == {card.links!r}")
