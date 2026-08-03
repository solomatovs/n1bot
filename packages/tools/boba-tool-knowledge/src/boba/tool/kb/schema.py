"""Создание схемы KB при старте приложения.

Так же, как chainlit data layer и langgraph checkpointer создают свои
таблицы: DDL идемпотентный, применяется на каждом запуске.
"""

from __future__ import annotations

import logging

from psycopg import Connection, sql
from psycopg.errors import InsufficientPrivilege

from boba.tool.kb.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.migrations import Migrations
from boba.tool.kb.postgres import KbPool

__all__ = ["KbSchema"]

logger = logging.getLogger(__name__)


class KbSchema:
    """Приводит схему базы знаний к актуальному виду."""

    def __init__(self, cfg: PostgresKnowledgeBaseConfig) -> None:
        self._cfg = cfg

    def _ensure_schema(self, conn: Connection) -> None:
        """Схема под таблицы KB; без прав на CREATE считаем, что её завёл админ."""
        name = self._cfg.tables.pg_schema
        try:
            conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(name)),
                prepare=False,
            )
        except InsufficientPrivilege:
            logger.info(
                "no permission for CREATE SCHEMA %r, "
                "assuming an administrator created it",
                name,
            )

    def setup(self) -> None:
        """Создать схему, применить миграции и векторный индекс под модель."""
        pool = KbPool.open(self._cfg.connection)
        with pool.connection() as conn:
            self._ensure_schema(conn)
            Migrations.apply_bootstrap(conn, schema_cfg=self._cfg.tables)

        with pool.connection() as conn:
            Migrations.ensure_vector_index(
                conn, dim=self._cfg.embedding.dim, schema_cfg=self._cfg.tables
            )

        logger.info(
            "KB schema ready: schema=%s chunks=%s dim=%d",
            self._cfg.tables.pg_schema,
            self._cfg.tables.chunks_table,
            self._cfg.embedding.dim,
        )
