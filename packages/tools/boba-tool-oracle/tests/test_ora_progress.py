"""Ход работы ora_query (pytest -m integration): подключение, отправка
запроса, ответ сервера, вычитка строк и фиксация отчётами тела.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from ora_tool_stand import IxStand

from boba.db.oracle.connection import OracleConfig
from boba.stand_core.progress import HeardProgress
from boba.tool.ora.tools import ora_query
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture(scope="module")
def connection(ix_stand: IxStand) -> OracleConfig:
    return ix_stand.ora_sources[0].oracle


@pytest.fixture
def heard() -> Iterator[HeardProgress]:
    pipe = HeardProgress()
    yield pipe
    pipe.close()


async def test_every_step_of_the_query_is_reported(
    connection: OracleConfig, heard: HeardProgress
) -> None:
    body = ToolMain.toolset(ora_query)[0].coroutine
    if body is None:
        raise AssertionError("ora_query is an async tool")

    sql = "select 7 as answer,\n       'x' as mark\n  from dual"
    result = await body(
        connection=connection, sql=sql, offset=0, limit=10, progress=heard.progress
    )
    if not result.ok:
        raise AssertionError(f"the query succeeds: {result.llm_view()}")

    messages = heard.messages()
    if messages[0] != "connecting to oracle, waiting for the server":
        raise AssertionError(f"the wait for the connection is named: {messages}")

    if not messages[1].startswith("connected to oracle in "):
        raise AssertionError(f"the connection is confirmed: {messages[1]}")
    if ": server " not in messages[1]:
        raise AssertionError(f"the server names its version: {messages[1]}")

    flat = "select 7 as answer, 'x' as mark from dual"
    sent = f"sending {len(sql)} chars to oracle and waiting for the answer: {flat}"
    if messages[2] != sent:
        raise AssertionError(f"the sent text is named on one line: {messages[2]}")

    answered = messages[3]
    if not answered.startswith("oracle answered in "):
        raise AssertionError(f"the answer of the server is reported: {answered}")
    if not answered.endswith(" with 2 columns (answer, mark), reading rows"):
        raise AssertionError(f"the columns of the answer are named: {answered}")

    done = messages[-1]
    if not done.startswith("read 1 rows from oracle in "):
        raise AssertionError(f"the rows read are counted: {done}")
    if not done.endswith("rows 1-1; end of result"):
        raise AssertionError(f"the window of the answer is named: {done}")

    for message in messages:
        if connection.host in message or connection.trace() in message:
            raise AssertionError(f"no address or login in a report: {message}")
