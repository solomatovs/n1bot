"""Потолки ответа pg_query на настоящем сервере: секция [tool.pg] сервиса
задаёт, сколько строк и байт отдаёт один вызов, и запрос сверх потолка
отказывается с числом потолка, а не обрезается молча.

Профиль соединения — сервисная секция [postgres] конфига стенда.
"""

from __future__ import annotations

import pytest

from boba.config import bind
from boba.db.postgres.connection import PostgresConfig
from boba.tool.pg.tools import PgToolConfig, pg_query
from boba.toolkit.entry import ToolMain
from boba.toolkit.failure import ToolRefusalError
from boba.toolkit.ports import ToolProgress
from boba.toolkit.result import SqlResult
from boba.toolkit.sql import SqlCeiling

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture(scope="module")
def connection(raw_config) -> PostgresConfig:
    return bind(raw_config, path="postgres", model=PostgresConfig)


@pytest.fixture(scope="module")
def ceilings(raw_config) -> PgToolConfig:
    """Потолки из секции [tool.pg] конфига сервиса — те, что задал администратор."""
    return bind(raw_config, path=PgToolConfig.SECTION, model=PgToolConfig)


class TestQueryCeiling:
    async def _query(
        self, connection: PostgresConfig, cfg: PgToolConfig, sql: str, limit: int
    ) -> SqlResult:
        body = ToolMain.toolset(pg_query)[0].coroutine
        if body is None:
            raise AssertionError("pg_query is an async tool")

        return await body(
            connection=connection,
            sql=sql,
            offset=0,
            limit=limit,
            cfg=cfg,
            progress=ToolProgress(-1),
        )

    async def test_window_of_the_ceiling_is_served_whole(
        self, connection: PostgresConfig, ceilings: PgToolConfig
    ) -> None:
        rows = ceilings.max_rows
        sql = f"select n from generate_series(1, {rows + 10}) as n"  # noqa: S608 — число из конфига

        result = await self._query(connection, ceilings, sql, rows)

        served = result.statements[0].rows
        if served is None or len(served) != rows:
            raise AssertionError(f"the window of {rows} rows is served, got {served}")

    async def test_limit_above_the_ceiling_is_refused_with_the_ceiling(
        self, connection: PostgresConfig, ceilings: PgToolConfig
    ) -> None:
        rows = ceilings.max_rows

        with pytest.raises(ToolRefusalError) as refused:
            await self._query(connection, ceilings, "select 1 as one", rows + 1)

        if refused.value.kind != SqlCeiling.ROWS:
            raise AssertionError(f"the refusal names the rows ceiling: {refused.value}")
        if f"ceiling of {rows} rows" not in str(refused.value):
            raise AssertionError(f"the refusal names the number: {refused.value}")

    async def test_answer_above_the_byte_ceiling_is_refused(
        self, connection: PostgresConfig, ceilings: PgToolConfig
    ) -> None:
        size = ceilings.max_bytes
        sql = f"select repeat('x', {size + 1}) as big"

        with pytest.raises(ToolRefusalError) as refused:
            await self._query(connection, ceilings, sql, 1)

        if refused.value.kind != SqlCeiling.BYTES:
            raise AssertionError(
                f"the refusal names the bytes ceiling: {refused.value}"
            )
        if f"ceiling of {size} bytes" not in str(refused.value):
            raise AssertionError(f"the refusal names the number: {refused.value}")
