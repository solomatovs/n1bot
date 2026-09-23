"""Ручной прогон ora-инструментов: функции вызываются напрямую.

Профиль соединения в бою подаёт хост из строк пользователя; здесь он
берётся из первого источника [ix_stand].ora_sources и передаётся параметром.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from ora_tool_stand import IxStand

from boba.db.oracle.connection import OracleConfig
from boba.tool.ora.tools import ora_list_tables, ora_query
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    SQL: ClassVar[str] = "select 1 as answer from dual"


@pytest.fixture(scope="module")
def connection(ix_stand: IxStand) -> OracleConfig:
    return ix_stand.ora_sources[0].oracle


async def test_run_ora_query(connection: OracleConfig) -> None:
    body = ToolMain.toolset(ora_query)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    artifact = await body(connection=connection, sql=RunArgs.SQL, offset=0, limit=50)

    print(artifact.llm_view())


async def test_run_ora_list_tables(connection: OracleConfig) -> None:
    body = ToolMain.toolset(ora_list_tables)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    artifact = await body(connection=connection, schema_name=None, offset=0, limit=50)

    print(artifact.llm_view())
