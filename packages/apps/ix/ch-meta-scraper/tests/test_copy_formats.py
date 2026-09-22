"""Сложные типы ClickHouse через TabSeparated в COPY ix: скаляры, Bool, DateTime64,
UUID, Nullable, Enum и строки со спецсимволами проходят как есть, Array/Tuple/Map
через toJSONString в jsonb.

Ошибки стенда: IxStandError — секции [ix_stand] нет, модуль пропускается.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path

import pytest
from ch_scraper_stand import IxStand

from boba.ch_meta_scraper.worker import ChSource
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.postgres import AsyncPostgresPool
from boba.ix_core.scrape import copy_blocks
from boba.stand.scraper import ScraperStandDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = IxStand.required()

DDL = """
create table zz_fmt.probe (
    i Int32, big Int64, num Decimal(18,4), dbl Float64, flag Bool, s String,
    d Date, dt DateTime('UTC'), dt64 DateTime64(6,'UTC'), u UUID,
    arr Array(String), nested Array(Array(Int32)), t Tuple(String, Int32),
    m Map(String, Int32), maybe Nullable(String), lc LowCardinality(String),
    e Enum8('a'=1,'b'=2)
) engine = Memory
"""
INSERT = """
insert into zz_fmt.probe values (
    1, 9007199254740993, 12345.6789, 1.5, true,
    'tab\\there "q" \\'s\\' back\\\\slash\\nnewline ünï',
    '2024-02-29', '2024-02-29 13:14:15', '2024-02-29 13:14:15.123456',
    '550e8400-e29b-41d4-a716-446655440000',
    ['a b','q"uote','back\\\\slash','comma,','','NULL','тест'], [[1,2],[3,4]],
    ('x',1), {'k':1,'k2':2}, NULL, 'lc', 'a'
)
"""
QUERY = """
select
    i, big, num, dbl, flag, s, d, dt, dt64, u,
    toJSONString(arr) as arr, toJSONString(nested) as nested,
    toJSONString(t) as t, toJSONString(m) as m, maybe, lc, e
from zz_fmt.probe
"""
RAW = """
create temp table raw_fmt (
    i int, big bigint, num numeric(18,4), dbl double precision, flag boolean, s text,
    d date, dt timestamp, dt64 timestamp, u uuid,
    arr jsonb, nested jsonb, t jsonb, m jsonb, maybe text, lc text, e text
)
"""


class TestCopyFormats:
    @pytest.mark.parametrize(
        "name", [source.name for source in STAND.ch_sources if source.demo]
    )
    async def test_complex_types_survive_the_block_copy(
        self,
        name: str,
        ix_stand: IxStand,
        ix_database: ScraperStandDatabase,
        tmp_path: Path,
    ) -> None:
        source = ix_stand.source(name)
        async with PayloadClickHouse.opened_config(source.admin) as client:
            await client.command("create database if not exists zz_fmt")
            await client.command("drop table if exists zz_fmt.probe")
            await client.command(DDL)
            await client.command(INSERT)

        query = tmp_path / "fmt.sql"
        query.write_text(QUERY, encoding="utf-8")
        try:
            async with (
                await AsyncPostgresPool.dedicated(ix_stand.ix_profile) as ix,
                ChSource(source.admin).open_session() as session,
            ):
                await ix.execute("set timezone to 'UTC'")
                await ix.execute(RAW)
                async with session.fetch_blocks("fmt", query, {}) as blocks:
                    await copy_blocks(ix, "raw_fmt", blocks)

                cur = await ix.execute(
                    "select i, big, num, flag, s, d, dt, dt64, u, "
                    "array(select jsonb_array_elements_text(arr)), nested, t, m, "
                    "maybe, lc, e from raw_fmt"
                )
                got = await cur.fetchone()
        finally:
            async with PayloadClickHouse.opened_config(source.admin) as client:
                await client.command("drop database if exists zz_fmt")

        assert got is not None
        assert got[0] == 1
        assert got[1] == 9007199254740993
        assert str(got[2]) == "12345.6789"
        assert got[3] is True
        assert got[4] == "tab\there \"q\" 's' back\\slash\nnewline ünï"
        assert got[5] == datetime.date(2024, 2, 29)
        assert got[6] == datetime.datetime(2024, 2, 29, 13, 14, 15)
        assert got[7] == datetime.datetime(2024, 2, 29, 13, 14, 15, 123456)
        assert got[8] == uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
        assert got[9] == ["a b", 'q"uote', "back\\slash", "comma,", "", "NULL", "тест"]
        assert got[10] == [[1, 2], [3, 4]]
        assert got[11] == ["x", 1]
        assert got[12] == {"k": 1, "k2": 2}
        assert got[13] is None
        assert got[14] == "lc"
        assert got[15] == "a"
