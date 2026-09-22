"""Сложные типы источника PostgreSQL через блоки `COPY TO STDOUT` в COPY ix: массивы,
jsonb, bytea, timestamptz, boolean, numeric, uuid и строки со спецсимволами проходят
байт в байт при явных настройках сессии (UTC, ISO, hex).

Ошибки стенда: IxStandError — секции [ix_stand] нет, модуль пропускается.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path

import pytest
from scraper_stand import IxStand

from boba.db.postgres import AsyncPostgresPool
from boba.ix_core.scrape import copy_blocks
from boba.pg_meta_scraper.worker import PgSource, WorkerConfig
from boba.stand.scraper import ScraperStandDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = IxStand.required()

DDL = """
create table fmt_probe (
    i int, big bigint, num numeric(18,4), dbl double precision, flag boolean,
    s text, d date, ts timestamp, tstz timestamptz, u uuid,
    arr text[], nested int[][], j jsonb, raw bytea, maybe text
)
"""
ROW = (
    1,
    9007199254740993,
    "12345.6789",
    1.5,
    True,
    "tab\there \"q\" 's' back\\slash\nnewline ünï",
    datetime.date(2024, 2, 29),
    datetime.datetime(2024, 2, 29, 13, 14, 15, 123456),
    datetime.datetime(
        2024, 2, 29, 13, 14, 15, tzinfo=datetime.timezone(datetime.timedelta(hours=3))
    ),
    uuid.UUID("550e8400-e29b-41d4-a716-446655440000"),
    ["a b", 'q"uote', "back\\slash", "comma,", "", "NULL", "тест"],
    [[1, 2], [3, 4]],
    '{"k": "v\\"q", "n": [1, 2, {"x": null}], "t": "tab\\t"}',
    b"\x00\x01\xff",
    None,
)


class TestCopyFormats:
    async def test_complex_types_survive_the_block_copy(
        self, ix_stand: IxStand, ix_database: ScraperStandDatabase, tmp_path: Path
    ) -> None:
        source = ix_stand.sources[-1]
        async with await AsyncPostgresPool.dedicated(source.demo_profile) as demo:
            await demo.execute("drop table if exists fmt_probe")
            await demo.execute(DDL)
            await demo.execute(
                "insert into fmt_probe values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s::jsonb, %s, %s)",
                ROW,
            )

        query = tmp_path / "fmt.sql"
        query.write_text("select * from fmt_probe", encoding="utf-8")
        async with (
            await AsyncPostgresPool.dedicated(ix_stand.ix_profile) as ix,
            PgSource(
                WorkerConfig(source=source.demo_profile)
            ).open_session() as session,
        ):
            await ix.execute("set timezone to 'UTC'")
            await ix.execute(f"create temp table raw_fmt {DDL[DDL.index('(') :]}")
            async with session.fetch_blocks("fmt", query, {}) as blocks:
                await copy_blocks(ix, "raw_fmt", blocks)

            cur = await ix.execute("select * from raw_fmt")
            got = await cur.fetchone()

        assert got is not None
        assert got[0] == 1
        assert got[1] == 9007199254740993
        assert str(got[2]) == "12345.6789"
        assert got[4] is True
        assert got[5] == ROW[5]
        assert got[6] == ROW[6]
        assert got[7] == ROW[7]
        assert got[8] == ROW[8]
        assert got[9] == ROW[9]
        assert got[10] == ROW[10]
        assert got[11] == ROW[11]
        assert got[12] == {"k": 'v"q', "n": [1, 2, {"x": None}], "t": "tab\t"}
        assert bytes(got[13]) == ROW[13]
        assert got[14] is None
