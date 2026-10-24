"""Юнит-тесты AsyncPostgresPool: kwargs конструктора, open/close, post-close-ошибка."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from boba.db.postgres import (
    AsyncPostgresPool,
    PostgresPoolClosedError,
    PostgresPoolLoopError,
)
from boba.db.postgres.connection import PostgresConfig


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
        {
            "host": "h",
            "dbname": "test",
            "auth": {"method": "trust", "user": "u"},
            **extra,
        }
    )


def test_pool_passes_cfg_to_connection_pool():
    AsyncPostgresPool(_cfg())
    if len(_FakeAsyncConnectionPool.instances) != 1:
        raise AssertionError("len(_FakeAsyncConnectionPool.instances) == 1")
    kwargs = _FakeAsyncConnectionPool.instances[0].kwargs
    if kwargs["open"] is not False:
        raise AssertionError('kwargs["open"] is False')
    if kwargs["connection_class"].__name__ != "AuthConnection[trust]":
        raise AssertionError(
            'kwargs["connection_class"].__name__ == "AuthConnection[trust]"'
        )
    conn = kwargs["kwargs"]
    if conn["host"] != "h":
        raise AssertionError('conn["host"] == "h"')
    if conn["dbname"] != "test":
        raise AssertionError('conn["dbname"] == "test"')
    if conn["user"] != "u":
        raise AssertionError('conn["user"] == "u"')
    if conn["autocommit"] is not True:
        raise AssertionError('conn["autocommit"] is True')
    if kwargs["min_size"] != 1:
        raise AssertionError('kwargs["min_size"] == 1')
    if kwargs["timeout"] != 2.0:
        raise AssertionError('kwargs["timeout"] == 2.0')


async def _open_close_twice(pool: AsyncPostgresPool) -> None:
    await pool.open()
    await pool.close()
    await pool.close()


def test_open_close():
    pool = AsyncPostgresPool(_cfg())
    inner = _FakeAsyncConnectionPool.instances[0]
    # close идемпотентен
    asyncio.run(_open_close_twice(pool))
    if inner.opened is not True:
        raise AssertionError("inner.opened is True")
    if inner.closed is not True:
        raise AssertionError("inner.closed is True")


def test_close_from_another_loop_raises():
    pool = AsyncPostgresPool(_cfg())
    inner = _FakeAsyncConnectionPool.instances[0]
    asyncio.run(pool.open())
    with pytest.raises(PostgresPoolLoopError):
        asyncio.run(pool.close())
    if inner.closed is not False:
        raise AssertionError("a pool of another loop stays open")


async def _open_then_close_all(own: AsyncPostgresPool) -> None:
    await own.open()
    await AsyncPostgresPool.close_all()


def test_close_all_closes_only_the_pools_of_the_running_loop():
    # пул другого цикла (поток хранилища) закрывает его владелец, не close_all
    foreign = AsyncPostgresPool(_cfg())
    own = AsyncPostgresPool(_cfg())
    foreign_inner, own_inner = _FakeAsyncConnectionPool.instances
    asyncio.run(foreign.open())
    asyncio.run(_open_then_close_all(own))
    if own_inner.closed is not True:
        raise AssertionError("the pool of the running loop is closed")
    if foreign_inner.closed is not False:
        raise AssertionError("the pool of another loop stays open")
    if foreign not in AsyncPostgresPool.opened():
        raise AssertionError("the pool of another loop stays live")


def test_connection_after_close_raises():
    pool = AsyncPostgresPool(_cfg())
    asyncio.run(pool.close())
    with pytest.raises(PostgresPoolClosedError):
        asyncio.run(pool.connection().__aenter__())


def test_connection_from_another_loop_raises():
    # каждый asyncio.run — свой loop; пул открыт в первом, обращение идёт из второго
    pool = AsyncPostgresPool(_cfg())
    asyncio.run(pool.open())
    with pytest.raises(PostgresPoolLoopError):
        asyncio.run(pool.connection().__aenter__())


def test_connection_in_same_loop_passes():
    pool = AsyncPostgresPool(_cfg())

    async def open_and_use() -> None:
        await pool.open()
        async with pool.connection():
            pass

    asyncio.run(open_and_use())
