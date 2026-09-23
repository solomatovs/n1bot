"""Сложные типы Oracle через пачки Arrow и CSV в COPY ix: NUMBER целые (в том числе
шире 2^53) и с дробью, BINARY_DOUBLE, DATE, TIMESTAMP, CLOB, RAW, строки со
спецсимволами и NULL. RAW запрос отдаёт `rawtohex`, дробный NUMBER без точности —
`to_char`, как в файлах scrape/, остальное идёт как есть. Таблицу создаёт EDGE_DEMO,
читает её сессия воркера под учёткой скрапера (у неё грант на sys.registry$ для
версии сервера).

Ошибки стенда: IxStandError — секции [ix_stand] нет, модуль пропускается.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from ora_scraper_stand import IxStand

from boba.db.oracle.payload import PayloadOracle
from boba.db.postgres import AsyncPostgresPool
from boba.ix_core.scrape import copy_blocks
from boba.ora_meta_scraper.worker import OraSource
from boba.stand.scraper import ScraperStandDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = IxStand.required()

DDL = """
create table edge_demo.fmt_probe (
    i number(10), big number(18), n206 number(20,6), nfree number, wide number,
    fl binary_double,
    s varchar2(200), d date, ts timestamp(6), c clob, r raw(4), maybe varchar2(10)
)
"""
INSERT = """
insert into edge_demo.fmt_probe values (
    1, 9007199254740993, 1/7, 1/7, 18014398526259208, 1.5,
    'tab' || chr(9) || 'here "q" ''s'' back\\slash' || chr(10) || 'newline ünï',
    date '2024-02-29', timestamp '2024-02-29 13:14:15.123456',
    to_clob('clob text'), hextoraw('00ff'), null
)
"""
QUERY = """
select
    i, big, n206, to_char(nfree) as nfree, wide, fl,
    s, d, ts, c, rawtohex(r) as r, maybe
from edge_demo.fmt_probe
"""
RAW = """
create temp table raw_fmt (
    i int, big bigint, n206 numeric(20,6), nfree numeric, wide bigint,
    fl double precision,
    s text, d timestamp, ts timestamp, c text, r text, maybe text
)
"""


class TestCopyFormats:
    @pytest.mark.parametrize(
        "name", [source.name for source in STAND.ora_sources if source.demo]
    )
    async def test_complex_types_survive_the_block_copy(
        self,
        name: str,
        ix_stand: IxStand,
        ix_database: ScraperStandDatabase,
        tmp_path: Path,
    ) -> None:
        source = ix_stand.source(name)
        payload = PayloadOracle(source.demo_owner)
        async with payload.opened() as owner:
            drop = (
                "begin execute immediate 'drop table edge_demo.fmt_probe'; "
                "exception when others then null; end;"
            )
            async with payload.rows(owner, drop):
                pass
            grant = f"grant select on edge_demo.fmt_probe to {source.oracle.auth.user}"
            for statement in (DDL, INSERT, "commit", grant):
                async with payload.rows(owner, statement):
                    pass

        query = tmp_path / "fmt.sql"
        query.write_text(QUERY, encoding="utf-8")
        async with (
            await AsyncPostgresPool.dedicated(ix_stand.ix_profile) as ix,
            OraSource(source.oracle).open_session() as session,
        ):
            await ix.execute("set timezone to 'UTC'")
            await ix.execute(RAW)
            async with session.fetch_blocks("fmt", query, {}) as blocks:
                await copy_blocks(ix, "raw_fmt", blocks)

            cur = await ix.execute("select * from raw_fmt")
            got = await cur.fetchone()

        assert got is not None
        assert got[0] == 1
        assert got[1] == 9007199254740993
        assert str(got[2]) == "0.142857"
        assert str(got[3]).startswith("0.1428571428571428571428571428571428571")
        assert got[4] == 18014398526259208
        assert got[5] == 1.5
        assert got[6] == "tab\there \"q\" 's' back\\slash\nnewline ünï"
        assert got[7] == datetime.datetime(2024, 2, 29, 0, 0)
        assert got[8] == datetime.datetime(2024, 2, 29, 13, 14, 15, 123456)
        assert got[9] == "clob text"
        assert got[10] == "00FF"
        assert got[11] is None
