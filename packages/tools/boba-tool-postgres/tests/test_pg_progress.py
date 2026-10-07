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


async def test_stages_and_server_messages_are_reported(
    connection: PostgresConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ToolProgress, "INTERVAL_SEC", 0.0)
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

    messages = [report.message for report in reports]
    expected = [
        "connecting",
        "connected, executing the statement",
        "NOTICE: loading stage one",
        "WARNING: stage two is slow",
        "statement 1 done: DO",
        "statement 2 done: SELECT 1",
    ]
    if messages != expected:
        raise AssertionError(f"stages and server messages in order: {messages}")

    done = [report.done for report in reports]
    if done != sorted(set(done)):
        raise AssertionError(f"the progress only grows: {done}")
