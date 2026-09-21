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
from typing import Any, ClassVar, Self

import psycopg
import pytest
from psycopg import sql
from pydantic import BaseModel, ConfigDict, ValidationError

from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.profile import PostgresConfig
from boba.ix_core.database import IxDatabase
from boba.ix_core.main import SCHEMA_DIR as CORE_SCHEMA_DIR
from boba.ix_core.schema_name import SchemaName
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

    SECTION: ClassVar[str] = "ix_stand"

    postgres: PostgresConfig
    krb: KerberosWorkspaceConfig
    database: str
    db_schema: str
    embedding_cache_dir: str

    @classmethod
    def load(cls) -> Self:
        path = ConfigLocator.path()
        stand_path = path.parent / StandLayers.FILE
        if not stand_path.is_file():
            raise IxStandError(f"ix stand: {stand_path} not found")

        raw = StandLayers.compose(path)

        try:
            return bind(raw, path=cls.SECTION, model=cls)
        except ValidationError as exc:
            raise IxStandError(
                f"ix stand: [{cls.SECTION}] in {stand_path}: {exc}"
            ) from exc

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
        return IxDatabase(
            db_schema=self.db_schema, postgres=self.ix_profile, krb=self.krb
        )


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
            await conn.execute(
                sql.SQL("drop database if exists {} with (force)").format(
                    sql.Identifier(self._stand.database)
                )
            )
            await conn.execute(
                sql.SQL("create database {}").format(
                    sql.Identifier(self._stand.database)
                )
            )

        database = self._stand.ix_database
        await SchemaUpgrade(CORE_SCHEMA_DIR, requires_core=False).run(database)
        for schema_dir in schema_dirs:
            await SchemaUpgrade(schema_dir).run(database)

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[psycopg.AsyncConnection[Any], None]:
        async with await AsyncPostgresPool.dedicated(self._stand.ix_profile) as conn:
            yield conn

    def render(self, text: str) -> sql.Composed:
        """Запрос проверки под схему графа: в тексте она стоит плейсхолдером."""
        return SchemaName.render(text, self._stand.db_schema)

    async def scalar(self, text: str, params: dict[str, Any]) -> Any:
        """Одно значение запроса проверки; отсутствие строки — ошибка стенда."""
        async with self.connection() as conn:
            cur = await conn.execute(self.render(text), params)
            row = await cur.fetchone()

        if row is None:
            raise IxStandError(f"ix stand: expected one row from {text!r}, got none")

        return row[0]
