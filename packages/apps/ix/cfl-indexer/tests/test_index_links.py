"""Ссылки между страницами на заглушке: рёбра cfl_page_link по id и по заголовку,
замена при правке, каскад при удалении цели.

Ошибки стенда: IxStandError — секции [ix_stand] нет, модуль пропускается.
"""

from __future__ import annotations

from typing import Any

import pytest
from cfl_stand import StubIndexer

from boba.stand.confluence import ConfluenceStub, StubPage, StubSpace
from boba.stand.ix import IxStandDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SPACE = "DEV"
HOME_HTML = (
    '<p>See <a href="/display/DEV/OrdersTable">orders</a>, '
    '<a href="/pages/viewpage.action?pageId=101">the same page</a>, '
    '<a href="/pages/viewpage.action?pageId=102">glossary</a>, '
    '<a href="/pages/viewpage.action?pageId=100">myself</a> and '
    '<a href="https://example.com/x">outside</a>.</p>'
)


def seed(stub: ConfluenceStub) -> None:
    stub.describe(StubSpace(key=SPACE, name="Data platform"))
    stub.add(StubPage(id="100", space=SPACE, title="Home", html=HOME_HTML))
    stub.add(
        StubPage(
            id="101",
            space=SPACE,
            title="OrdersTable",
            html='<p>Back to <a href="/display/DEV/Home">home</a>.</p>',
        )
    )
    stub.add(StubPage(id="102", space=SPACE, title="Glossary", html="<p>Terms.</p>"))


EDGES = """
    select s.address->>'content', t.address->>'content', l.kind
    from {schema}.edge e
    join {schema}.cfl_page_link l on l.edge_id = e.id
    join {schema}.node s on s.id = e.node_src_id
    join {schema}.node t on t.id = e.node_tgt_id
    order by 1, 2
"""


async def edges(database: IxStandDatabase) -> list[tuple[Any, ...]]:
    async with database.connection() as conn:
        cur = await conn.execute(database.render(EDGES))

        return [tuple(row) for row in await cur.fetchall()]


class TestIndexLinks:
    async def test_links_become_edges(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, _ = stub
        seed(fake)

        reports = await stub_indexer.run(SPACE)

        assert reports[0].linked == 3
        assert await edges(ix_database) == [
            ("100", "101", "id"),
            ("100", "102", "id"),
            ("101", "100", "title"),
        ]

    async def test_edit_replaces_and_delete_cascades(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, _ = stub
        seed(fake)
        await stub_indexer.run(SPACE)

        fake.pages["100"].edit(
            html='<p>Only <a href="/display/DEV/Glossary">terms</a>.</p>'
        )
        reports = await stub_indexer.run(SPACE)
        assert reports[0].linked == 1
        assert await edges(ix_database) == [
            ("100", "102", "title"),
            ("101", "100", "title"),
        ]

        fake.delete("102")
        await stub_indexer.run(SPACE)
        assert await edges(ix_database) == [("101", "100", "title")]
