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


def _cfg(
    host: str = "localhost",
    dbname: str = "test",
    **extra: Any,
) -> PostgresConfig:
    return PostgresConfig.model_validate(
        {"host": host, "user": "u", "dbname": dbname, **extra}
    )


def test_pool_passes_cfg_to_connection_pool():
    PostgresPool(_cfg())
    assert len(_FakeConnectionPool.instances) == 1
    kwargs = _FakeConnectionPool.instances[0].kwargs
    conn = kwargs["kwargs"]
    assert conn["host"] == "localhost"
    assert conn["dbname"] == "test"
    assert conn["user"] == "u"
    assert conn["autocommit"] is True
    # prepare_threshold=None (отключить prepared) не попадает в kwargs
    assert "prepare_threshold" not in conn
    assert kwargs["min_size"] == 1
    assert kwargs["timeout"] == 2.0


def test_pool_applies_override_options():
    PostgresPool(_cfg(), override_options={"default_transaction_read_only": "on"})
    kwargs = _FakeConnectionPool.instances[0].kwargs
    assert "default_transaction_read_only=on" in kwargs["kwargs"]["options"]


def test_get_returns_singleton_for_same_cfg():
    a = PostgresPool.get(_cfg())
    b = PostgresPool.get(_cfg())
    assert a is b
    assert len(_FakeConnectionPool.instances) == 1


def test_get_creates_new_pool_for_different_cfg():
    a = PostgresPool.get(_cfg(dbname="a"))
    b = PostgresPool.get(_cfg(dbname="b"))
    assert a is not b
    assert len(_FakeConnectionPool.instances) == 2


def test_get_creates_new_pool_for_different_override_options():
    a = PostgresPool.get(_cfg(), override_options={"a": "1"})
    b = PostgresPool.get(_cfg(), override_options={"b": "2"})
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
