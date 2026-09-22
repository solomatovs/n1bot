"""Стенд индексатора Confluence: база ix из [ix_stand] stand.toml, заглушка Confluence
из boba-stand и конфиг индексатора, собранный на них.

Ошибки:
IxStandError — секция [ix_stand] отсутствует или неполна.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from cfl_stand import PACKAGE_DIR, SharedIndexers, StubIndexer
from psycopg import sql

from boba.db.postgres.query import PgQueryBuilder
from boba.ix_fts import worker as fts
from boba.ix_trgm import worker as trgm
from boba.ix_vector import worker as vector
from boba.stand.confluence import ConfluenceStub, LiveServer
from boba.stand.doc import DocStand
from boba.stand.ix import IxStand, IxStandDatabase


@pytest.fixture(scope="session")
def ix_stand() -> IxStand:
    return IxStand.required()


@pytest.fixture(scope="session")
def doc_stand() -> DocStand:
    return DocStand.required()


@pytest.fixture(scope="session")
async def ix_database(ix_stand: IxStand) -> IxStandDatabase:
    database = IxStandDatabase(ix_stand)
    await database.recreate(
        [
            Path(fts.__file__).resolve().parent / "schema",
            Path(trgm.__file__).resolve().parent / "schema",
            Path(vector.__file__).resolve().parent / "schema",
            PACKAGE_DIR / "schema",
        ]
    )

    return database


@pytest.fixture
async def stub() -> AsyncIterator[tuple[ConfluenceStub, int]]:
    fake = ConfluenceStub()
    async with LiveServer(fake.app()) as server:
        yield fake, server.port


@pytest.fixture
def shared(ix_stand: IxStand, ix_database: IxStandDatabase) -> SharedIndexers:
    return SharedIndexers(ix_stand)


@pytest.fixture
def stub_indexer(
    ix_stand: IxStand, ix_database: IxStandDatabase, stub: tuple[ConfluenceStub, int]
) -> StubIndexer:
    _, port = stub

    return StubIndexer(ix_stand, port)


CLEAN = """
    with gone as (
        delete from {schema}.node where surface::varchar like 'cfl_%%' returning id
    ),
    trgm as (
        delete from {schema}.ix_trgm
        where node_id in (select id from gone) returning 1
    ),
    fts as (
        delete from {schema}.ix_fts
        where node_id in (select id from gone) returning 1
    ),
    emb as (
        delete from {schema}.ix_emb_e5_1024
        where node_id in (select id from gone) returning 1
    )
    select count(*) from gone
"""


@pytest.fixture(autouse=True)
async def clean_graph(ix_database: IxStandDatabase) -> None:
    """Каждый сценарий начинает с пустого графа Confluence: заглушка каждого теста
    живёт на своём порту, то есть это другой сервер с теми же ключами."""
    async with ix_database.connection() as conn:
        query = (
            PgQueryBuilder(schema=sql.Identifier(ix_database.stand.db_schema))
            .add(CLEAN)
            .build()
        )
        await conn.execute(query.text, query.params)
