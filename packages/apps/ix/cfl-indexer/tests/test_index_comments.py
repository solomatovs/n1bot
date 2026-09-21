"""Комментарии страницы на заглушке: node под страницей, текст в индексе, правка и
удаление.

Ошибки стенда: IxStandError — секции [ix_stand] нет, модуль пропускается.
"""

from __future__ import annotations

from typing import Any

import pytest
from cfl_stand import StubIndexer

from boba.stand.confluence import (
    ConfluenceStub,
    StubComment,
    StubPage,
    StubRoute,
    StubSpace,
)
from boba.stand.ix import IxStandDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SPACE = "DEV"


def seed(stub: ConfluenceStub) -> StubPage:
    stub.describe(StubSpace(key=SPACE, name="Data platform"))
    page = StubPage(id="400", space=SPACE, title="Design", html="<p>Design.</p>")
    page.comments.append(
        StubComment(id="c1", html="<p>Please add the <b>retention</b> policy.</p>")
    )
    page.comments.append(StubComment(id="c2", html="<p>Done.</p>", location="inline"))
    stub.add(page)

    return page


COMMENT_NODES = """
    select count(*)
    from {schema}.node n
    join {schema}.tree t on t.node_id = n.id
    join {schema}.cfl_page p on p.node_id = t.parent_id
    where n.surface = 'cfl_comment' and p.content_id = %(content)s
"""
FTS_ROWS = """
    select aspect::varchar, content
    from {schema}.cfl_idx_fts f
    join {schema}.node n on n.id = f.node_id
    where n.address->>'comment' = %(comment)s
    order by aspect
"""
COMMENT_ROW = """
    select version, location, author, length(content_hash)
    from {schema}.cfl_comment where comment_id = %(comment)s
"""


async def rows(
    database: IxStandDatabase, text: str, params: dict[str, Any]
) -> list[Any]:
    async with database.connection() as conn:
        cur = await conn.execute(database.render(text), params)

        return list(await cur.fetchall())


class TestIndexComments:
    async def test_comments_become_nodes_under_the_page(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, _ = stub
        seed(fake)

        reports = await stub_indexer.run(SPACE)

        assert reports[0].seen == 4
        assert await ix_database.scalar(COMMENT_NODES, {"content": "400"}) == 2

        text = dict(await rows(ix_database, FTS_ROWS, {"comment": "c1"}))
        assert text["body"] == "Please add the **retention** policy."
        assert text["card"].startswith("Comment by stub.commenter in DEV\n")
        assert "title" not in text

        version, location, author, digest = (
            await rows(ix_database, COMMENT_ROW, {"comment": "c2"})
        )[0]
        assert (version, location, author, digest) == (
            1,
            "inline",
            "stub.commenter",
            64,
        )

    async def test_edit_and_delete(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, _ = stub
        page = seed(fake)
        await stub_indexer.run(SPACE)
        fake.reset_calls()

        reports = await stub_indexer.run(SPACE)
        assert fake.calls[StubRoute.COMMENTS] == 1
        assert reports[0].unchanged == 4

        page.comments[0].edit("<p>Retention added.</p>")
        reports = await stub_indexer.run(SPACE)
        assert reports[0].indexed == 1
        text = dict(await rows(ix_database, FTS_ROWS, {"comment": "c1"}))
        assert text["body"] == "Retention added."

        page.comments.pop()
        reports = await stub_indexer.run(SPACE)
        assert reports[0].swept == 1
        assert await ix_database.scalar(COMMENT_NODES, {"content": "400"}) == 1
