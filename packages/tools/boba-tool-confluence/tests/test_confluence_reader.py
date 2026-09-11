"""Цепочка страницы целиком: HTML -> LocalConfluenceReader -> чанки.

Реальный ридер и реальный чанкер, без подмен: проверяется то, ради чего
таблица и карточка вообще появились — связь колонки со значением доживает
до чанка, а страница получает карточку с оглавлением и метками.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from boba.confluence.models import ConfluenceKeys, TableShape
from boba.indexing import (
    Chunk,
    ChunkStream,
    Metadata,
    RawDocument,
    ReaderKeys,
    Section,
    SectionKeys,
    SectionKind,
    SourceId,
)
from boba.tool.confluence.chunking import ChunkerParams, StructuralChunkerFactory
from boba.tool.confluence.ingest_tools import LocalConfluenceReader

pytestmark = pytest.mark.anyio

_HTML = """
<html><body>
<h1>Настройки клиента</h1><p>Клиент читает параметры из конфига.</p>
<table><caption>Таймауты</caption>
<tr><th>Параметр</th><th>Значение</th><th>Описание</th></tr>
<tr><td>connect_timeout</td><td>30s</td><td>установка соединения</td></tr>
<tr><td>read_timeout</td><td>60s</td><td>чтение ответа</td></tr>
<tr><td>retry_attempts</td><td>3</td><td>повторы запроса</td></tr>
</table>
<h2>Ограничения</h2><p>Больше трёх повторов не делаем.</p>
</body></html>
"""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _document(html: str) -> RawDocument:
    meta = Metadata.empty()
    meta = meta.set(ReaderKeys.PAGE_TITLE, "Клиент")
    meta = meta.set(ConfluenceKeys.PAGE_ID, "1")
    meta = meta.set(ConfluenceKeys.LABELS, ("infra", "howto"))
    meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, ("База знаний", "Сервисы"))
    return RawDocument(
        handle=ChunkStream.of(html.encode("utf-8")),
        source_id=SourceId("https://confluence/rest/api/content/1"),
        metadata=meta,
    )


async def _sections(html: str) -> AsyncIterator[Section[str]]:
    shape = TableShape(row_layout_max_columns=4, row_layout_min_rows=3)
    reader = LocalConfluenceReader(shape)
    async for section in reader.read(_document(html)):
        yield section


async def _chunks(chunk_size: int = 4000, html: str = _HTML) -> list[Chunk[str]]:
    params = ChunkerParams(
        chunk_size=chunk_size,
        chunk_overlap=0,
        table_shape=TableShape(row_layout_max_columns=4, row_layout_min_rows=3),
    )
    chunker = StructuralChunkerFactory.build(params)
    rows: list[Chunk[str]] = []
    async for chunk in chunker.chunk(_sections(html)):
        rows.append(chunk)

    return rows


def _of_kind(chunks: list[Chunk[str]], kind: SectionKind) -> list[Chunk[str]]:
    rows: list[Chunk[str]] = []
    for chunk in chunks:
        if chunk.metadata.get(SectionKeys.KIND) == str(kind):
            rows.append(chunk)

    return rows


class TestPageChunks:
    async def test_card_opens_the_page(self) -> None:
        chunks = await _chunks()
        cards = _of_kind(chunks, SectionKind.CARD)
        if len(cards) != 1:
            raise AssertionError("страница должна дать ровно одну карточку")

        text = cards[0].format_content
        for expected in ("Page: Клиент", "Location: База знаний", "Sections"):
            if expected not in text:
                raise AssertionError(f"карточка без {expected!r}: {text!r}")

    async def test_table_rows_carry_their_column_names(self) -> None:
        chunks = await _chunks()
        tables = _of_kind(chunks, SectionKind.TABLE)
        if not tables:
            raise AssertionError("таблица не дошла до чанков")

        joined = "\n".join(chunk.format_content for chunk in tables)
        if "Параметр: connect_timeout" not in joined:
            raise AssertionError(f"строка потеряла имя колонки: {joined!r}")
        if "Значение: 30s" not in joined:
            raise AssertionError(f"строка потеряла значение: {joined!r}")

    async def test_table_text_does_not_leak_into_prose(self) -> None:
        """Ячейки не должны второй раз приехать плоским текстом секции."""
        chunks = await _chunks()
        for chunk in chunks:
            kind = chunk.metadata.get(SectionKeys.KIND)
            if kind == str(SectionKind.TABLE):
                continue

            if kind == str(SectionKind.CARD):
                continue

            if "connect_timeout" in chunk.format_content:
                raise AssertionError(f"текст секции повторяет таблицу: {chunk!r}")

    async def test_table_chunk_keeps_caption_and_columns(self) -> None:
        chunks = await _chunks()
        table = _of_kind(chunks, SectionKind.TABLE)[0]
        if table.metadata.get(SectionKeys.TABLE_CAPTION) != "Таймауты":
            raise AssertionError("подпись таблицы не попала в metadata")

        columns = table.metadata.get(SectionKeys.TABLE_COLUMNS)
        if columns != ("Параметр", "Значение", "Описание"):
            raise AssertionError(f"колонки таблицы не попали в metadata: {columns!r}")

    async def test_labels_become_tags_and_leave_metadata(self) -> None:
        """Метки — колонка tags: она индексируется и умеет HasTag/HasAnyTag."""
        chunks = await _chunks()
        for chunk in chunks:
            if chunk.tags != frozenset({"infra", "howto"}):
                raise AssertionError(f"метки не доехали тегами: {chunk.tags!r}")

            if chunk.metadata.has(ConfluenceKeys.LABELS):
                raise AssertionError("метки продублированы в metadata чанка")

    async def test_table_row_survives_a_tiny_chunk_size(self) -> None:
        """Бюджет меньше таблицы: строки раскладываются по чанкам целиком."""
        chunks = await _chunks(chunk_size=160)
        tables = _of_kind(chunks, SectionKind.TABLE)
        if len(tables) < 2:
            raise AssertionError("маленький бюджет должен был разложить таблицу")

        rows = (
            "Параметр: connect_timeout; Значение: 30s; Описание: установка соединения",
            "Параметр: read_timeout; Значение: 60s; Описание: чтение ответа",
            "Параметр: retry_attempts; Значение: 3; Описание: повторы запроса",
        )
        joined = "\n".join(chunk.format_content for chunk in tables)
        for row in rows:
            if row not in joined:
                raise AssertionError(f"строка таблицы порвана: {row!r} в {joined!r}")

    async def test_prose_keeps_heading_path(self) -> None:
        chunks = await _chunks()
        for chunk in chunks:
            if chunk.metadata.get(SectionKeys.KIND) != str(SectionKind.SECTION):
                continue

            if not chunk.metadata.get(SectionKeys.HEADING_PATH):
                raise AssertionError("текстовый чанк без хлебных крошек")


_LINK_TITLES = tuple(
    f"Linked page number {index} with a fairly long title" for index in range(60)
)


def _linked_html() -> str:
    """Страница-индекс: пара разделов и шестьдесят ссылок на другие страницы."""
    anchors: list[str] = []
    for index, title in enumerate(_LINK_TITLES):
        href = f"/confluence/spaces/S/pages/{100 + index}/Page+{index}"
        anchors.append(f'<li><a href="{href}">{title}</a></li>')

    items = "".join(anchors)
    return (
        "<html><body>"
        "<h1>Назначение</h1><p>Индекс предложений.</p>"
        "<h2>Список</h2>"
        f"<ul>{items}</ul>"
        "</body></html>"
    )


class TestCardLinks:
    """Длинный список ссылок не раздувает первый чанк карточки и не рвётся."""

    async def test_long_link_list_spills_into_more_card_chunks(self) -> None:
        chunks = await _chunks(chunk_size=1000, html=_linked_html())
        cards = _of_kind(chunks, SectionKind.CARD)
        if len(cards) < 2:
            raise AssertionError(
                f"ждал несколько чанков карточки, получил {len(cards)}"
            )

        for card in cards:
            if not card.format_content.startswith("Page: Клиент\n"):
                raise AssertionError(f"чанк карточки без строки Page: {card!r}")

            if len(card.format_content) > 1000:
                raise AssertionError(f"чанк карточки сверх бюджета: {card!r}")

    async def test_outline_comes_before_links_in_the_first_chunk(self) -> None:
        chunks = await _chunks(chunk_size=1000, html=_linked_html())
        first = _of_kind(chunks, SectionKind.CARD)[0].format_content
        for expected in ("Location: База знаний", "Sections:", "- Список"):
            if expected not in first:
                raise AssertionError(f"шапка без {expected!r}: {first!r}")

        if "Links:" in first and first.index("Sections:") > first.index("Links:"):
            raise AssertionError(f"ссылки встали перед оглавлением: {first!r}")

    async def test_every_link_title_survives_whole(self) -> None:
        chunks = await _chunks(chunk_size=1000, html=_linked_html())
        joined = "\n".join(
            card.format_content for card in _of_kind(chunks, SectionKind.CARD)
        )
        for title in _LINK_TITLES:
            if f"- {title}\n" not in f"{joined}\n":
                raise AssertionError(f"название ссылки порвано или потеряно: {title!r}")
