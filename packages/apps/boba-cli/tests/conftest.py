"""Фикстуры стенда `boba acp`, общие для тестов команды, бинарника и
истории в Postgres: фейковая модель и сервер MCP по streamable HTTP."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from test_cli_acp import FakeModel, McpStand


@pytest.fixture
async def model() -> AsyncIterator[FakeModel]:
    fake = FakeModel()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def mcp() -> AsyncIterator[McpStand]:
    stand = McpStand()
    await stand.start()
    try:
        yield stand
    finally:
        await stand.stop()
