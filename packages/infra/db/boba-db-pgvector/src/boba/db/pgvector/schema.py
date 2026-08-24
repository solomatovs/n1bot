"""Создание схемы KB при старте приложения: идемпотентный DDL на каждом запуске."""

from __future__ import annotations

import logging

from psycopg import AsyncConnection, sql
from psycopg.errors import InsufficientPrivilege

from boba.db.pgvector.migrations import Migrations
from boba.db.pgvector.store import KbPool, PostgresStoreConfig

__all__ = ["KbSchema"]

logger = logging.getLogger(__name__)


class KbSchema:
    """Приводит схему базы знаний к актуальному виду."""

    def __init__(self, cfg: PostgresStoreConfig, *, dim: int) -> None:
        self._cfg = cfg
        self._dim = dim

    async def _ensure_schema(self, conn: AsyncConnection) -> None:
        """Схема под таблицы KB; без прав на CREATE считаем, что её завёл админ."""
        name = self._cfg.tables.pg_schema
        await conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(name)),
            prepare=False,
        )

    async def setup(self) -> None:
        """Создать схему, применить миграции и векторный индекс под модель."""
        pool = await KbPool.open(self._cfg.connection)
        async with pool.connection() as conn:
            try:
                await self._ensure_schema(conn)
                await Migrations.apply_bootstrap(conn, schema_cfg=self._cfg.tables)
            except InsufficientPrivilege:
                logger.info(
                    "no permission operation"
                    "assuming an administrator created it",
                )

        async with pool.connection() as conn:
            try:
                await Migrations.ensure_vector_index(
                    conn, dim=self._dim, schema_cfg=self._cfg.tables
                )
            except InsufficientPrivilege:
                logger.info(
                    "no permission operation"
                    "assuming an administrator created it",
                )

        logger.info(
            "KB schema ready: schema=%s chunks=%s dim=%d",
            self._cfg.tables.pg_schema,
            self._cfg.tables.chunks_table,
            self._dim,
        )
