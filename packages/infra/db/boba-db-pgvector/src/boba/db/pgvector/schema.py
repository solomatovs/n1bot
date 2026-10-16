"""Создание схемы KB при старте приложения: идемпотентный DDL на каждом запуске.

Ошибки:
KbMigrationError — размерность вектора негодна.
PostgresError — пул недоступен, каталога файлов схемы нет или сервер отклонил
    файл.
psycopg.Error — сервер отклонил векторный индекс.
"""

from __future__ import annotations

import logging
from importlib.resources import files

from boba.db.pgvector.config import PostgresStoreConfig
from boba.db.pgvector.migrations import Migrations
from boba.db.postgres import AsyncPostgresPool, PostgresSchema

__all__ = ["KbSchema"]

logger = logging.getLogger(__name__)


class KbSchema:
    """Приводит схему базы знаний к актуальному виду: файлы schema/*.sql
    пакета под замком DDL и векторный индекс под размерность модели. Здесь
    только DDL, векторных значений не летает, поэтому берётся общий пул
    процесса, а не отдельный пул store с адаптером vector."""

    def __init__(self, cfg: PostgresStoreConfig, *, dim: int) -> None:
        self._cfg = cfg
        self._dim = dim
        self._schema = PostgresSchema(cfg.tables.pg_schema)
        self._migrations = Migrations(cfg.tables)

    async def setup(self) -> None:
        schema_dir = files("boba.db.pgvector") / "schema"
        names = self._migrations.names()
        pool = await AsyncPostgresPool.get(self._cfg.connection)
        async with pool.connection() as conn:
            await self._schema.apply_files(conn, schema_dir, names)
            await self._migrations.ensure_vector_index(conn, self._dim)

        logger.info(
            "KB schema ready: schema=%s chunks=%s dim=%d",
            self._cfg.tables.pg_schema,
            self._cfg.tables.chunks_table,
            self._dim,
        )
