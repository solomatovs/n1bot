"""База ix из секции конфига приложения: схема графа, профиль postgres, рабочий
каталог kerberos и пул соединений на прогон.

Каждое приложение ix (ядро, скрапер, индексаторы, описатель, стенд поиска) держит
в своей секции одни и те же поля: `db_schema`, `postgres` — профиль boba-db-postgres
с выбором способа авторизации, опциями сессии и параметрами пула — и `krb` с
krb5.conf и каталогом кэшей билетов. IxDatabase — общая модель этих полей, IxPool
открывает по ней AsyncPostgresPool и отдаёт пул или одно соединение из него.

Ошибки:
IxDatabaseError — каталог кэшей kerberos не подготовить или пул не отдал соединение.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.db.postgres.profile import PostgresConfig
from boba.krb import KerberosWorkspaceConfig

__all__ = ["IxDatabase", "IxDatabaseError", "IxPool"]


class IxDatabaseError(Exception):
    """База ix недоступна: kerberos-каталог или соединение из пула."""


class IxDatabase(BaseModel):
    """Общие поля секции приложения ix; секция приложения наследует модель."""

    model_config = ConfigDict(extra="ignore")

    db_schema: str = Field(min_length=1)
    postgres: PostgresConfig
    krb: KerberosWorkspaceConfig


class IxPool:
    """Пул к базе ix на один прогон приложения.

    Ставит рабочий каталог kerberos секции, открывает AsyncPostgresPool по профилю
    и закрывает его на выходе из блока. Воркеры берут одно соединение session()
    на весь цикл, http-стенд держит пул opened() и берёт соединение на запрос.
    """

    @staticmethod
    @asynccontextmanager
    async def opened(database: IxDatabase) -> AsyncGenerator[AsyncPostgresPool, None]:
        try:
            database.krb.apply()
        except OSError as exc:
            msg = (
                f"ix database {database.postgres.where()}: preparing kerberos "
                f"cache dir {database.krb.ccache_dir} failed: {exc}"
            )
            raise IxDatabaseError(msg) from exc

        pool = AsyncPostgresPool(database.postgres)
        await pool.open()

        try:
            yield pool
        finally:
            await pool.close()

    @classmethod
    @asynccontextmanager
    async def session(
        cls, database: IxDatabase
    ) -> AsyncGenerator[psycopg.AsyncConnection[Any], None]:
        async with AsyncExitStack() as stack:
            pool = await stack.enter_async_context(cls.opened(database))

            try:
                conn = await stack.enter_async_context(pool.connection())
            except (psycopg.Error, PostgresError) as exc:
                msg = (
                    f"ix database {database.postgres.where()} as "
                    f"{database.postgres.trace()}: no connection from the pool: {exc}"
                )
                raise IxDatabaseError(msg) from exc

            yield conn
