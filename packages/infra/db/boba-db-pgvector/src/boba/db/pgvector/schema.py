"""Создание схемы KB при старте приложения: идемпотентный DDL на каждом запуске.

Ошибки:
KbMigrationError — миграций нет или размерность вектора негодна.
PostgresError — пул недоступен.
psycopg.Error — DDL отклонён не по правам.
"""

from __future__ import annotations

import logging

from psycopg.errors import InsufficientPrivilege

from boba.db.pgvector.config import PostgresStoreConfig
from boba.db.pgvector.migrations import Migrations
from boba.db.postgres import AsyncPostgresPool, PostgresSchema

__all__ = ["KbSchema"]

logger = logging.getLogger(__name__)


class KbSchema:
    """Приводит схему базы знаний к актуальному виду: схема, миграции под
    замком DDL и векторный индекс под размерность модели. Здесь только DDL,
    векторных значений не летает, поэтому берётся общий пул процесса, а не
    отдельный пул store с адаптером vector. Без прав на DDL считаем, что
    администратор всё завёл сам."""

    def __init__(self, cfg: PostgresStoreConfig, *, dim: int) -> None:
        self._cfg = cfg
        self._dim = dim
        self._schema = PostgresSchema(cfg.tables.pg_schema)
        self._migrations = Migrations(cfg.tables)

    async def setup(self) -> None:
        pool = await AsyncPostgresPool.get(self._cfg.connection)
        async with pool.connection() as conn:
            try:
                # несколько процессов стартуют разом: DDL под одним advisory-lock,
                # иначе каталог отвечает «tuple concurrently updated»
                async with conn.transaction():
                    await self._schema.ddl_lock().acquire(conn)
                    await self._schema.ensure(conn)
                    await self._migrations.apply(conn)
            except InsufficientPrivilege as exc:
                logger.info(
                    "no permission for the kb migrations in schema %s, assuming an "
                    "administrator applied them: %s",
                    self._schema.name,
                    exc,
                )

        async with pool.connection() as conn:
            try:
                await self._migrations.ensure_vector_index(conn, self._dim)
            except InsufficientPrivilege as exc:
                logger.info(
                    "no permission for the kb vector index in schema %s, assuming an "
                    "administrator created it: %s",
                    self._schema.name,
                    exc,
                )

        logger.info(
            "KB schema ready: schema=%s chunks=%s dim=%d",
            self._cfg.tables.pg_schema,
            self._cfg.tables.chunks_table,
            self._dim,
        )
