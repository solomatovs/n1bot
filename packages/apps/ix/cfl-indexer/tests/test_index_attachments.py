"""Вложения страницы на заглушке: файл идёт в индекс текстом, отсечённые остаются
метаданными, перезаливка переиндексирует, удалённое сметается.

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
    StubAttachment,
    StubPage,
    StubRoute,
    StubSpace,
)
from boba.stand.ix import IxStandDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SPACE = "DEV"
NOTES = b"Runbook for the ETL_loader job.\nRestart it from the scheduler."


def seed(stub: ConfluenceStub) -> StubPage:
    stub.describe(StubSpace(key=SPACE, name="Data platform"))
    page = StubPage(id="300", space=SPACE, title="Runbooks", html="<p>Runbooks.</p>")
    page.attachments.append(
        StubAttachment(
            id="att1", title="etl.txt", media_type="text/plain", content=NOTES
        )
    )
    page.attachments.append(
        StubAttachment(
            id="att2", title="diagram.png", media_type="image/png", content=b"\x89PNG"
        )
    )
    page.attachments.append(
        StubAttachment(
            id="att3",
            title="dump.bin",
            media_type="application/octet-stream",
            content=b"1",
        )
    )
    stub.add(page)

    return page


ATTACHMENT_NODES = """
    select count(*)
    from {schema}.node n
    join {schema}.tree t on t.node_id = n.id
    join {schema}.cfl_page p on p.node_id = t.parent_id
    where n.surface = 'cfl_attachment' and p.content_id = %(content)s
"""
FTS_ROWS = """
    select aspect::varchar, content
    from {schema}.ix_fts f
    join {schema}.node n on n.id = f.node_id
    where n.address->>'attachment' = %(attachment)s
    order by aspect
"""
ATTACHMENT_ROW = """
    select version, content_hash, media_type, file_size
    from {schema}.cfl_attachment where attachment_id = %(attachment)s
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


class TestIndexAttachments:
    async def test_files_become_nodes_under_the_page(
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

        assert reports[0].seen == 5
        assert await ix_database.scalar(ATTACHMENT_NODES, {"content": "300"}) == 3

        text = dict(await rows(ix_database, FTS_ROWS, {"attachment": "att1"}))
        assert text["body"] == NOTES.decode("utf-8")
        assert text["title"] == "etl.txt"
        assert text["path"] == "DEV/etl.txt"
        assert text["card"].startswith("Attachment DEV/etl.txt (text/plain, ")

        version, content_hash, media_type, size = (
            await rows(ix_database, ATTACHMENT_ROW, {"attachment": "att1"})
        )[0]
        assert version == 1
        assert len(content_hash) == 64
        assert media_type == "text/plain"
        assert size == len(NOTES)

        image = dict(await rows(ix_database, FTS_ROWS, {"attachment": "att2"}))
        assert "body" not in image
        assert "ocr" not in image
        assert image["title"] == "diagram.png"

        binary = dict(await rows(ix_database, FTS_ROWS, {"attachment": "att3"}))
        assert "body" not in binary
        assert binary["title"] == "dump.bin"

    async def test_reupload_reindexes_and_same_bytes_do_not(
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
        assert fake.calls[StubRoute.DOWNLOAD] == 0
        assert reports[0].unchanged == 5

        page.attachment("etl.txt").upload(
            b"Runbook v2.", when="2026-02-01T00:00:00.000Z"
        )
        reports = await stub_indexer.run(SPACE)
        assert fake.calls[StubRoute.DOWNLOAD] == 1
        text = dict(await rows(ix_database, FTS_ROWS, {"attachment": "att1"}))
        assert text["body"] == "Runbook v2."
        version = (await rows(ix_database, ATTACHMENT_ROW, {"attachment": "att1"}))[0][
            0
        ]
        assert version == 2

        fake.reset_calls()
        page.attachment("etl.txt").upload(
            b"Runbook v2.", when="2026-03-01T00:00:00.000Z"
        )
        reports = await stub_indexer.run(SPACE)
        assert fake.calls[StubRoute.DOWNLOAD] == 1
        assert reports[0].indexed == 0
        assert reports[0].unchanged == 5
        text = dict(await rows(ix_database, FTS_ROWS, {"attachment": "att1"}))
        assert text["body"] == "Runbook v2."

    async def test_removed_attachment_is_swept(
        self,
        stub: tuple[ConfluenceStub, int],
        stub_indexer: StubIndexer,
        ix_database: IxStandDatabase,
    ) -> None:
        fake, _ = stub
        page = seed(fake)
        await stub_indexer.run(SPACE)

        page.attachments.pop(0)
        reports = await stub_indexer.run(SPACE)

        assert reports[0].swept == 1
        assert await ix_database.scalar(ATTACHMENT_NODES, {"content": "300"}) == 2
        assert await rows(ix_database, FTS_ROWS, {"attachment": "att1"}) == []
