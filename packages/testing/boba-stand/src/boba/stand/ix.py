"""Стенд базы ix для тестов приложений ix: секция [ix_stand] stand.toml и база
прогонов, которую тест пересоздаёт с ядром ix-core и схемами нужных пакетов.

Модель секции — общая часть: профиль сервера, kerberos-каталог, имя базы прогонов,
схема графа и кэш моделей эмбеддинга; стенд конкретного пакета наследует её и
добавляет своё (источники скрапера, спейсы Confluence).

Ошибки:
IxStandError — секция [ix_stand] отсутствует или неполна, база стенда недоступна.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self

import psycopg
import pytest
from psycopg import sql
from pydantic import BaseModel, ConfigDict, ValidationError

from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.database import IxDatabase
from boba.ix_core.main import SCHEMA_DIR as CORE_SCHEMA_DIR
from boba.ix_core.registry import IxRegistry
from boba.ix_core.upgrade import SchemaUpgrade
from boba.krb import KerberosWorkspaceConfig
from boba.runtime.config import ConfigLocator
from boba.stand.site import StandLayers

__all__ = ["IxStand", "IxStandDatabase", "IxStandError"]


class IxStandError(Exception):
    """Конфиг стенда ix недоступен или неполон."""


class IxStand(BaseModel):
    """Секция [ix_stand]: сервер ix, имя базы прогонов, схема графа, kerberos и кэш
    моделей эмбеддинга; лишние ключи секции принадлежат стендам пакетов."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    postgres: PostgresConfig
    krb: KerberosWorkspaceConfig
    database: str
    db_schema: str
    embedding_cache_dir: str

    @classmethod
    def load(cls) -> Self:
        section = "ix_stand"
        path = ConfigLocator.path()
        stand_path = path.parent / StandLayers.FILE
        if not stand_path.is_file():
            raise IxStandError(f"ix stand: {stand_path} not found")

        raw = StandLayers.compose(path)

        try:
            stand = bind(raw, path=section, model=cls)
        except ValidationError as exc:
            raise IxStandError(f"ix stand: [{section}] in {stand_path}: {exc}") from exc

        stand.krb.apply()

        return stand

    @classmethod
    def required(cls) -> Self:
        try:
            return cls.load()
        except IxStandError as exc:
            pytest.skip(str(exc), allow_module_level=True)

    @property
    def ix_profile(self) -> PostgresConfig:
        """Профиль базы прогонов на сервере ix."""
        return self.postgres.model_copy(update={"dbname": self.database})

    @property
    def ix_database(self) -> IxDatabase:
        """Секция базы ix глазами пакетов: схема, профиль базы прогонов, kerberos."""
        return IxDatabase(db_schema=self.db_schema, postgres=self.ix_profile)


class IxStandDatabase:
    """База ix стенда: пересоздаётся с ядром ix-core и схемами переданных
    пакетов; соединение к ней — для проверок теста."""

    def __init__(self, stand: IxStand) -> None:
        self._stand = stand

    @property
    def stand(self) -> IxStand:
        return self._stand

    async def recreate(self, schema_dirs: Sequence[Path]) -> None:
        async with await AsyncPostgresPool.dedicated(self._stand.postgres) as conn:
            query = (
                PgQueryBuilder(db=sql.Identifier(self._stand.database))
                .add("drop database if exists {db} with (force)")
                .build()
            )
            await conn.execute(query.text, query.params)
            query = (
                PgQueryBuilder(db=sql.Identifier(self._stand.database))
                .add("create database {db}")
                .build()
            )
            await conn.execute(query.text, query.params)

        database = self._stand.ix_database
        await SchemaUpgrade(CORE_SCHEMA_DIR, requires_core=False).run(database)
        for schema_dir in schema_dirs:
            await SchemaUpgrade(schema_dir).run(database)

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[psycopg.AsyncConnection[Any], None]:
        async with await AsyncPostgresPool.dedicated(self._stand.ix_profile) as conn:
            yield conn

    async def urls(self) -> IxRegistry:
        """Формулы ссылок из реестра стенда: тест проверяет ими объявление владельца."""
        registry = IxRegistry(self._stand.db_schema)
        async with self.connection() as conn:
            await registry.read_urls(conn)

        return registry

    async def nodes(self) -> list[tuple[str, dict[str, Any]]]:
        """Поверхность и адрес каждой node: по ним тест собирает ссылки."""
        async with self.connection() as conn:
            query = (
                PgQueryBuilder(schema=sql.Identifier(self._stand.db_schema))
                .add("""
                    select
                        n.surface::varchar,
                        n.address
                    from
                        {schema}.node n
                    order by
                        n.id
                """)
                .build()
            )
            cur = await conn.execute(query.text, query.params)
            rows = await cur.fetchall()

        found: list[tuple[str, dict[str, Any]]] = []
        for surface, address in rows:
            found.append((str(surface), dict(address)))

        return found

    async def scalar(self, text: str, params: dict[str, Any]) -> Any:
        """Одно значение запроса проверки; отсутствие строки — ошибка стенда."""
        async with self.connection() as conn:
            query = (
                PgQueryBuilder(schema=sql.Identifier(self._stand.db_schema))
                .add(text, **params)
                .build()
            )
            cur = await conn.execute(query.text, query.params)
            row = await cur.fetchone()

        if row is None:
            raise IxStandError(f"ix stand: expected one row from {text!r}, got none")

        return row[0]


def peak_rss_mib() -> int:
    """Пик резидентной памяти этого процесса с момента exec, из VmHWM в
    /proc/self/status: ru_maxrss не годится, spawn-потомок наследует значение
    родителя на момент fork."""
    status = Path("/proc/self/status")
    for line in status.read_text(encoding="utf-8").splitlines():
        if not line.startswith("VmHWM:"):
            continue

        kib = int(line.split()[1])
        return kib >> 10

    raise IxStandError(f"reading {status}: expected a VmHWM line, got none")
