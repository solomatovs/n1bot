"""Общий каркас хранилищ каталога на Postgres: граница ошибок слоя с учётом
отказа домена снимков, замок по id подключения или процесса и максимум
колонки. ProcessStore и ConnectionStore наследуют его и добавляют свои
таблицы; пул, сборщик запросов, транзакция с курсором словарей, разбор строк
в модели и проверка returning приходят из PostgresTable.

Ошибки:
CatalogStoreError — Postgres недоступен, ответ битый, строка не
    складывается в модель.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import ClassVar
from uuid import UUID

from boba.catalog_service.config import CatalogConfig
from boba.catalog_service.records import CatalogStoreError
from boba.db.postgres import Cursor, PgQuery, PostgresPool, PostgresTable
from boba.db.postgres.catalog import CatalogDomainError

logger = logging.getLogger(__name__)

__all__ = ["CatalogStoreBase"]


class CatalogStoreBase(PostgresTable):
    """База хранилищ каталога: наследник объявляет свои таблицы и SQL, каркас
    переводит отказ базы и домена в CatalogStoreError и даёт замок по id."""

    LABEL: ClassVar[str] = "catalog"

    def __init__(
        self, cfg: CatalogConfig, schema: str, pool: PostgresPool | None = None
    ) -> None:
        postgres = cfg.connection
        if pool is None:
            postgres = cfg.require_conn()

        super().__init__(postgres, schema, pool)
        self._cfg = cfg

    def _failure(self, action: str, exc: Exception) -> Exception:
        return CatalogStoreError(self._detail(action, exc))

    @asynccontextmanager
    async def _guarded(self, action: str) -> AsyncGenerator[None, None]:
        """Граница слоя: отказ базы, пула или домена уходит наружу как
        CatalogStoreError."""
        try:
            async with super()._guarded(action):
                yield
        except CatalogDomainError as exc:
            raise self._failure(action, exc) from exc

    async def _lock(self, cur: Cursor, prefix: str, key: UUID) -> None:
        """Транзакционный advisory-замок по id в схеме; снимается с концом
        транзакции."""
        await self._advisory_lock(cur, f"{self.schema}.{prefix}.{key}")

    async def _max_of(self, cur: Cursor, query: PgQuery, what: str) -> int:
        """Наибольшее значение из запроса с колонкой top; 0 — строк нет."""
        await cur.execute(query.text, query.params)
        row = self._returning(await cur.fetchone(), what)

        return int(row["top"])
