"""Юнит-тесты PostgresPool: singleton-кэш, close-идемпотентность, post-close-ошибка."""

from __future__ import annotations

from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from boba.db.postgres import (
    PostgresConfig,
    PostgresPool,
    PostgresPoolClosedError,
)


class _FakeConnectionPool:
    """Стенд для psycopg_pool.ConnectionPool, чтобы не ходить в БД."""

    instances: ClassVar[list[_FakeConnectionPool]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        _FakeConnectionPool.instances.append(self)

    def connection(self) -> Any:
        cm = MagicMock()
        cm.__enter__.return_value = MagicMock(name="connection")
        cm.__exit__.return_value = False
        return cm

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _patch_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # psycopg_pool импортируется лениво внутри PostgresPool.__init__.
    import psycopg_pool

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakeConnectionPool)
    _FakeConnectionPool.instances.clear()
    PostgresPool._CACHE.clear()


def _cfg(dsn: str = "postgresql://localhost/test") -> PostgresConfig:
    return PostgresConfig(dsn=dsn)


def test_pool_passes_cfg_to_connection_pool():
    PostgresPool(_cfg())
    assert len(_FakeConnectionPool.instances) == 1
    kwargs = _FakeConnectionPool.instances[0].kwargs
    assert kwargs["conninfo"] == "postgresql://localhost/test"
    assert kwargs["min_size"] == 1
    assert kwargs["max_size"] == 4
    assert kwargs["timeout"] == 10.0


def test_get_returns_singleton_for_same_cfg():
    a = PostgresPool.get(_cfg())
    b = PostgresPool.get(_cfg())
    assert a is b
    assert len(_FakeConnectionPool.instances) == 1


def test_get_creates_new_pool_for_different_dsn():
    a = PostgresPool.get(_cfg("postgresql://a/db"))
    b = PostgresPool.get(_cfg("postgresql://b/db"))
    assert a is not b
    assert len(_FakeConnectionPool.instances) == 2


def test_close_is_idempotent():
    pool = PostgresPool.get(_cfg())
    inner = _FakeConnectionPool.instances[0]
    pool.close()
    pool.close()
    assert inner.closed is True


def test_connection_after_close_raises():
    pool = PostgresPool.get(_cfg())
    pool.close()
    with pytest.raises(PostgresPoolClosedError):  # noqa: SIM117
        with pool.connection():
            pass


def test_get_recreates_pool_after_close():
    a = PostgresPool.get(_cfg())
    a.close()
    b = PostgresPool.get(_cfg())
    assert a is not b
    assert len(_FakeConnectionPool.instances) == 2
