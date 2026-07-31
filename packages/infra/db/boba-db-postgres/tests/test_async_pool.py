"""Юнит-тесты AsyncPostgresPool: kwargs конструктора, open/close, post-close-ошибка."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from boba.db.postgres import (
    AsyncPostgresPool,
    PostgresConfig,
    PostgresPoolClosedError,
)


class _FakeAsyncConnectionPool:
    """Стенд для psycopg_pool.AsyncConnectionPool, чтобы не ходить в БД."""

    instances: ClassVar[list[_FakeAsyncConnectionPool]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        _FakeAsyncConnectionPool.instances.append(self)

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    def connection(self) -> Any:
        cm = MagicMock()
        cm.__aenter__.return_value = MagicMock(name="async_connection")
        cm.__aexit__.return_value = False
        return cm


@pytest.fixture(autouse=True)
def _patch_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # psycopg_pool импортируется лениво внутри AsyncPostgresPool.__init__.
    import psycopg_pool

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _FakeAsyncConnectionPool)
    _FakeAsyncConnectionPool.instances.clear()


def _cfg(**extra: Any) -> PostgresConfig:
    return PostgresConfig.model_validate(
        {"host": "h", "user": "u", "dbname": "test", **extra}
    )


def test_pool_passes_cfg_to_connection_pool():
    AsyncPostgresPool(_cfg())
    assert len(_FakeAsyncConnectionPool.instances) == 1
    kwargs = _FakeAsyncConnectionPool.instances[0].kwargs
    assert kwargs["open"] is False
    assert kwargs["connection_class"].__name__ == "AsyncConnection"
    conn = kwargs["kwargs"]
    assert conn["host"] == "h"
    assert conn["dbname"] == "test"
    assert conn["user"] == "u"
    assert conn["autocommit"] is True
    assert kwargs["min_size"] == 1
    assert kwargs["timeout"] == 2.0


def test_pool_applies_override_options():
    AsyncPostgresPool(_cfg(), override_options={"search_path": "chainlit"})
    kwargs = _FakeAsyncConnectionPool.instances[0].kwargs
    assert "search_path=chainlit" in kwargs["kwargs"]["options"]


def test_open_close():
    pool = AsyncPostgresPool(_cfg())
    inner = _FakeAsyncConnectionPool.instances[0]
    asyncio.run(pool.open())
    assert inner.opened is True
    asyncio.run(pool.close())
    assert inner.closed is True
    # close идемпотентен
    asyncio.run(pool.close())


def test_connection_after_close_raises():
    pool = AsyncPostgresPool(_cfg())
    asyncio.run(pool.close())
    with pytest.raises(PostgresPoolClosedError):
        asyncio.run(pool.connection().__aenter__())
