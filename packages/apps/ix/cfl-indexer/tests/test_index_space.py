"""Обход спейса заглушки: граф, текст в общем полнотексте, отсечение по версии и
хэшу, чистка. Выводимые аспекты и векторы делают общие индексаторы, поэтому там, где
они проверяются, тест гоняет их следом за обходом.

Ошибки стенда: IxStandError — секции [ix_stand] нет, модуль пропускается.
"""

from __future__ import annotations

from typing import Any

import pytest
from cfl_stand import SharedIndexers, StubIndexer
from psycopg import sql

from boba.db.postgres.query import PgQueryBuilder
from boba.stand.confluence import (
    ConfluenceStub,
    StubKind,
    StubPage,
    StubRoute,
    StubSpace,
)
from boba.stand.ix import IxStandDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SPACE = "DEV"

HOME_HTML = "<h1>Home</h1><p>Welcome to the <b>data</b> platform.</p>"
CHILD_HTML = "<h2>Orders</h2><p>Table dm.orders keeps every order line.</p>"
BLOG_HTML = "<p>Release notes for the OrderBook_v2 pipeline.</p>"


def seed(stub: ConfluenceStub) -> None:
    stub.describe(StubSpace(key=SPACE, name="Data platform", description="Docs of DWH"))
    stub.add(StubPage(id="100", space=SPACE, title="Home", html=HOME_HTML))
    stub.add(
        StubPage(
            id="101",
            space=SPACE,
            title="OrdersTable",
            html=CHILD_HTML,
            parent="100",
            labels=["dwh", "etl"],
        )
    )
    stub.add(
        StubPage(
            id="200",
            space=SPACE,
            title="Release 2026-09",
            html=BLOG_HTML,
            kind=StubKind.BLOGPOST,
        )
    )


COUNT_BY_SURFACE = """
    select count(*) from {schema}.node where surface::varchar = %(surface)s
"""
FTS_ROWS = """
    select aspect::varchar, content
    from {schema}.ix_fts f
    join {schema}.node n on n.id = f.node_id
    where n.address->>'content' = %(content)s
    order by aspect
"""
TRGM_COUNT = """
    select count(*)
    from {schema}.ix_trgm f
    join {schema}.node n on n.id = f.node_id
    where n.address->>'content' = %(content)s
"""
EMB_HASHES = """
    select aspect::varchar || ':' || content_hash
    from {schema}.ix_emb_e5_1024 e
    join {schema}.node n on n.id = e.node_id
    where n.address->>'content' = %(content)s
    group by 1 order by 1
"""
PARENT_TITLE = """
    select p.title
    from {schema}.node n
    join {schema}.tree t on t.node_id = n.id
    join {schema}.cfl_page p on p.node_id = t.parent_id
    where n.address->>'content' = %(content)s
"""
PAGE_ROW = """
    select version, content_hash, labels, ancestor_titles
    from {schema}.cfl_page where content_id = %(content)s
"""


async def rows(
    database: IxStandDatabase, text: str, params: dict[str, Any]
) -> list[Any]:
    async with database.connection() as conn:
        query = (
            PgQueryBuilder(schema=sql.Identifier(database.stand.db_schema))
            .add(text, **params)
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        return list(await cur.fetchall())


class TestIndexSpace:
    async def test_space_lands_in_graph_and_shared_indexes(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        shared: SharedIndexers,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, _ = stub
        seed(fake)

        reports = await stub_indexer.run(SPACE)
        await shared.text()
        await shared.vectors()

        assert reports[0].seen == 4
        assert reports[0].indexed == 4
        assert reports[0].swept == 0

        for surface, expected in (
            ("cfl_space", 1),
            ("cfl_page", 2),
            ("cfl_blogpost", 1),
        ):
            got = await ix_database.scalar(COUNT_BY_SURFACE, {"surface": surface})
            assert got == expected, surface

        parent = await ix_database.scalar(PARENT_TITLE, {"content": "101"})
        assert parent == "Home"

        version, content_hash, labels, ancestors = (
            await rows(ix_database, PAGE_ROW, {"content": "101"})
        )[0]
        assert version == 1
        assert len(content_hash) == 64
        assert labels == ["dwh", "etl"]
        assert ancestors == ["Home"]

        fts = dict(await rows(ix_database, FTS_ROWS, {"content": "101"}))
        assert set(fts) == {"title", "path", "words", "labels", "card", "body"}
        assert fts["body"] == "## Orders\n\nTable dm.orders keeps every order line."
        assert fts["path"] == "DEV/OrdersTable"
        assert fts["words"] == "orders table"
        assert fts["labels"] == "dwh etl"
        assert fts["card"].startswith(
            "Page DEV/Home/OrdersTable\nLabels: dwh etl\n## Orders"
        )

        trgm = await ix_database.scalar(TRGM_COUNT, {"content": "101"})
        assert trgm == 3

        embedded = await rows(ix_database, EMB_HASHES, {"content": "101"})
        assert sorted(row[0].split(":")[0] for row in embedded) == [
            "body",
            "card",
            "labels",
        ]

    async def test_links_follow_surface_formulas(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, port = stub
        seed(fake)
        await stub_indexer.run(SPACE)

        urls = await ix_database.urls()
        built: dict[str, str] = {}
        for surface, address in await ix_database.nodes():
            built[surface] = urls.url_of(surface, address)

        origin = f"http://127.0.0.1:{port}"
        assert built["cfl_space"] == f"{origin}/display/{SPACE}"
        assert built["cfl_page"].startswith(f"{origin}/pages/viewpage.action?pageId=")
        assert built["cfl_blogpost"] == f"{origin}/pages/viewpage.action?pageId=200"

    async def test_second_run_fetches_no_bodies(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, _ = stub
        seed(fake)
        await stub_indexer.run(SPACE)
        fake.reset_calls()

        reports = await stub_indexer.run(SPACE)

        assert fake.calls[StubRoute.BODY] == 0
        assert reports[0].unchanged == 4
        assert reports[0].indexed == 0

    async def test_body_edit_rewrites_text_and_vector(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        shared: SharedIndexers,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, _ = stub
        seed(fake)
        await stub_indexer.run(SPACE)
        await shared.text()
        await shared.vectors()
        before = await rows(ix_database, EMB_HASHES, {"content": "101"})

        fake.pages["101"].edit(html="<p>Orders moved to dm.order_lines.</p>")
        reports = await stub_indexer.run(SPACE)
        await shared.text()
        await shared.vectors()

        assert reports[0].indexed == 1
        fts = dict(await rows(ix_database, FTS_ROWS, {"content": "101"}))
        assert fts["body"] == "Orders moved to dm.order_lines."
        after = await rows(ix_database, EMB_HASHES, {"content": "101"})
        assert after != before

    async def test_label_edit_keeps_body_vector(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        shared: SharedIndexers,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, _ = stub
        seed(fake)
        await stub_indexer.run(SPACE)
        await shared.text()
        await shared.vectors()
        before = dict(
            row[0].split(":")
            for row in await rows(ix_database, EMB_HASHES, {"content": "101"})
        )
        fake.reset_calls()

        fake.pages["101"].edit(labels=["dwh", "etl", "finance"])
        reports = await stub_indexer.run(SPACE)
        await shared.text()
        await shared.vectors()

        assert fake.calls[StubRoute.BODY] == 1
        assert reports[0].indexed == 1
        fts = dict(await rows(ix_database, FTS_ROWS, {"content": "101"}))
        assert fts["labels"] == "dwh etl finance"
        after = dict(
            row[0].split(":")
            for row in await rows(ix_database, EMB_HASHES, {"content": "101"})
        )
        assert after["body"] == before["body"]
        assert after["card"] != before["card"]

    async def test_deleted_content_is_swept(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        shared: SharedIndexers,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, _ = stub
        seed(fake)
        await stub_indexer.run(SPACE)
        await shared.text()
        await shared.vectors()

        fake.delete("200")
        reports = await stub_indexer.run(SPACE)
        await shared.text()
        await shared.vectors()

        assert reports[0].swept == 1
        assert (
            await ix_database.scalar(COUNT_BY_SURFACE, {"surface": "cfl_blogpost"}) == 0
        )
        assert await rows(ix_database, FTS_ROWS, {"content": "200"}) == []
        assert await rows(ix_database, EMB_HASHES, {"content": "200"}) == []
