"""Ход работы ch_query (pytest -m integration): подключение, отправка запроса,
ответ сервера и вычитка строк отчётами тела.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.stand_core.progress import HeardProgress
from boba.tool.ch.tools import ch_query
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture(scope="module")
def connection(raw_config) -> ClickHouseConfig:
    return bind(raw_config, path="clickhouse", model=ClickHouseConfig)


@pytest.fixture
def heard() -> Iterator[HeardProgress]:
    pipe = HeardProgress()
    yield pipe
    pipe.close()


async def test_every_step_of_the_query_is_reported(
    connection: ClickHouseConfig, heard: HeardProgress
) -> None:
    body = ToolMain.toolset(ch_query)[0].coroutine
    if body is None:
        raise AssertionError("ch_query is an async tool")

    sql = "select number as n,\n       toString(number) as s\n  from numbers(3)"
    result = await body(
        connection=connection, sql=sql, offset=0, limit=10, progress=heard.progress
    )
    if not result.ok:
        raise AssertionError(f"the query succeeds: {result.llm_view()}")

    messages = heard.messages()
    if messages[0] != "connecting to clickhouse, waiting for the server":
        raise AssertionError(f"the wait for the connection is named: {messages}")

    if not messages[1].startswith("connected to clickhouse in "):
        raise AssertionError(f"the connection is confirmed: {messages[1]}")
    if ": server " not in messages[1]:
        raise AssertionError(f"the server names its version: {messages[1]}")

    flat = "select number as n, toString(number) as s from numbers(3)"
    sent = f"sending {len(sql)} chars to clickhouse and waiting for the answer: {flat}"
    if messages[2] != sent:
        raise AssertionError(f"the sent text is named on one line: {messages[2]}")

    answered = messages[3]
    if not answered.startswith("clickhouse answered in "):
        raise AssertionError(f"the answer of the server is reported: {answered}")
    if not answered.endswith(" with 2 columns (n, s), reading rows"):
        raise AssertionError(f"the columns of the answer are named: {answered}")

    done = messages[-1]
    if not done.startswith("read 3 rows from clickhouse in "):
        raise AssertionError(f"the rows read are counted: {done}")
    if "query id " not in done or not done.endswith("rows 1-3; end of result"):
        raise AssertionError(f"the query id and the window are named: {done}")

    for message in messages:
        if str(connection.host) in message or connection.trace() in message:
            raise AssertionError(f"no address or login in a report: {message}")
