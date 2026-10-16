"""Создание схемы KB при старте плагина: идемпотентный DDL на каждом запуске.

Ошибки:
KbMigrationError — размерность вектора негодна.
PostgresError — соединение не открылось, каталога файлов схемы нет или сервер
    отклонил файл либо векторный индекс; текст называет базу, схему и
    расширения, без которых таблицы не создаются.
"""

from __future__ import annotations

import logging
from importlib.resources import files
from typing import ClassVar

import psycopg

from boba.db.pgvector.config import KnowledgeBaseSchemaConfig
from boba.db.pgvector.migrations import Migrations
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresSchema
from boba.toolkit.manifest import PluginStorage

__all__ = ["KbSchema"]

logger = logging.getLogger(__name__)


class KbSchema(PluginStorage):
    """Приводит схему базы знаний к актуальному виду: файлы schema/*.sql
    пакета под замком DDL и векторный индекс под размерность модели.

    Реализация PluginStorage: её объявляют стартом плагины kb и ingest, и
    boba-mcp зовёт setup на своём старте. Работает на одном выделенном
    соединении: процесс boba-mcp пулов не держит, а векторных значений здесь
    не летает.
    """

    EXTENSIONS: ClassVar[str] = "vector, pg_trgm, unaccent, btree_gin"
    """Расширения, которые создают файлы схемы: без прав суперпользователя их
    заводит администратор."""

    def __init__(self, cfg: KnowledgeBaseSchemaConfig) -> None:
        self._cfg = cfg
        self._schema = PostgresSchema(cfg.tables.pg_schema)
        self._migrations = Migrations(cfg.tables)

    async def setup(self) -> None:
        schema_dir = files("boba.db.pgvector") / "schema"
        names = self._migrations.names()
        where = (
            f"kb tables in schema {self._schema.name} of {self._cfg.connection.where()}"
        )
        try:
            conn = await AsyncPostgresPool.dedicated(self._cfg.connection)
        except psycopg.Error as exc:
            msg = f"{where}: connecting to the database failed: {exc}"
            raise PostgresError(msg) from exc

        async with conn:
            try:
                await self._schema.apply_files(conn, schema_dir, names)
            except PostgresError as exc:
                msg = (
                    f"{where}: {exc}; the extensions {self.EXTENSIONS} must exist "
                    "in the database: create them as superuser or create the "
                    "database from a template that has them"
                )
                raise PostgresError(msg) from exc

            try:
                await self._migrations.ensure_vector_index(
                    conn, self._cfg.embedding.dim
                )
            except psycopg.Error as exc:
                msg = f"{where}: creating the vector index failed: {exc}"
                raise PostgresError(msg) from exc

        logger.info(
            "KB schema ready: schema=%s chunks=%s dim=%d",
            self._cfg.tables.pg_schema,
            self._cfg.tables.chunks_table,
            self._cfg.embedding.dim,
        )
