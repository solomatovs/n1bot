"""Ход работы pg_query на настоящем сервере: стадии запроса и сообщения
сервера доходят отчётами, пока запрос идёт.

Профиль соединения — сервисная секция [postgres] конфига стенда.
"""

from __future__ import annotations

import os

import pytest

from boba.config import bind
from boba.db.postgres.connection import PostgresConfig
from boba.tool.pg.tools import pg_query
from boba.toolkit.entry import ToolMain
from boba.toolkit.ports import ProgressReport, ToolProgress

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCRIPT = """
do $$
begin
    raise notice 'loading stage one';
    perform pg_sleep(0.2);
    raise warning 'stage two is slow';
end
$$;
select 7 as answer
"""


@pytest.fixture(scope="module")
def connection(raw_config) -> PostgresConfig:
    return bind(raw_config, path="postgres", model=PostgresConfig)


def _check_messages(connection: PostgresConfig, messages: list[str]) -> None:
    """Каждый шаг запроса назван, адрес и вход в отчёты не попали."""
    if len(messages) != 8:
        raise AssertionError(f"every step of the query is reported: {messages}")

    if messages[0] != "connecting to postgres, waiting for the server":
        raise AssertionError(f"the wait for the connection is named: {messages[0]}")

    entered = messages[1]
    if not entered.startswith("connected to postgres in "):
        raise AssertionError(f"the connection is confirmed: {entered}")
    if ": server " not in entered or "backend pid " not in entered:
        raise AssertionError(f"the server introduces itself: {entered}")

    private = (str(connection.host), connection.trace())
    for message in messages:
        for value in private:
            if value in message:
                raise AssertionError(f"no address or login in a report: {message}")

    sent = f"sending {len(SCRIPT)} chars to postgres and waiting for the answer: do $$"
    if not messages[2].startswith(sent):
        raise AssertionError(f"the sent text is named: {messages[2]}")

    said = [
        "postgres says: NOTICE: loading stage one",
        "postgres says: WARNING: stage two is slow",
    ]
    if messages[3:5] != said:
        raise AssertionError(f"server messages arrive in order: {messages[3:5]}")

    outcomes = [
        ("postgres answered in ", ": statement 1: DO"),
        ("postgres answered in ", " with 1 columns (answer), reading rows"),
        (
            "read 1 rows from postgres in ",
            ": statement 2: SELECT 1, rows 1-1; end of result",
        ),
    ]
    for message, (head, tail) in zip(messages[5:], outcomes, strict=True):
        if not message.startswith(head) or not message.endswith(tail):
            raise AssertionError(f"the outcome of each statement: {message}")


async def test_stages_and_server_messages_are_reported(
    connection: PostgresConfig,
) -> None:
    body = ToolMain.toolset(pg_query)[0].coroutine
    if body is None:
        raise AssertionError("pg_query is an async tool")

    read_fd, write_fd = os.pipe()
    try:
        result = await body(
            connection=connection,
            sql=SCRIPT,
            offset=0,
            limit=10,
            progress=ToolProgress(write_fd),
        )
        os.close(write_fd)
        raw = b""
        while chunk := os.read(read_fd, 65536):
            raw += chunk
    finally:
        os.close(read_fd)

    if not result.ok:
        raise AssertionError(f"the script succeeds: {result.llm_view()}")

    reports: list[ProgressReport] = []
    for line in raw.decode("utf-8").splitlines():
        reports.append(ProgressReport.model_validate_json(line))

    _check_messages(connection, [report.message for report in reports])

    done = [report.done for report in reports]
    if done != sorted(set(done)):
        raise AssertionError(f"the progress only grows: {done}")
