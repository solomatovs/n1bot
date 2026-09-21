"""Накат схемы в базу ix: ядро пакетом ix-core, свои таблицы каждым пакетом.

Файлы schema/*.sql применяются по порядку имён, каждый файл одной командой
psycopg в autocommit; схема подставляется в `{schema}` перед отправкой. Файл
самодостаточен: значения enum, добавленные `alter type ... add value`, нельзя
использовать в своей же транзакции, поэтому пакет кладёт их отдельным файлом,
а их использование следующим. Файлы идемпотентны (`if not exists`,
`on conflict do nothing`), повторный накат безопасен.

После файлов проверяются все объявления аспектов {schema}.surface_aspect и все
строки реестра таблиц индексов {schema}.index_table: тело объявления выполняется
с limit 0 и сверяется с контрактом, таблица индекса — на существование и колонки
своего вида. Опечатка владельца валит его же накат, а не прогон потребителя.

Ошибки:
SchemaUpgradeError — база недоступна, каталога схемы нет, сервер отклонил DDL,
    ядро ix отсутствует там, где пакет на него опирается, объявление аспекта или
    строка реестра индексов нарушает контракт.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

import psycopg
from pydantic import BaseModel, ConfigDict

from boba.ix_core.aspects import (
    AspectContract,
    AspectDeclarationError,
    AspectDeclarations,
)
from boba.ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.ix_core.indexes import IndexTableError, IndexTables
from boba.ix_core.schema_name import SchemaName

__all__ = [
    "CoreTable",
    "SchemaUpgrade",
    "SchemaUpgradeError",
    "UpgradeReport",
]

logger = logging.getLogger("ix-upgrade")


class SchemaUpgradeError(Exception):
    """Схему не удалось применить."""


class CoreTable:
    """Таблица ядра, наличием которой пакет проверяет, что ядро уже накачено."""

    NODE: ClassVar[str] = "node"
    EDGE: ClassVar[str] = "edge"
    SURFACE_ASPECT: ClassVar[str] = "surface_aspect"
    INDEX_TABLE: ClassVar[str] = "index_table"


class UpgradeReport(BaseModel):
    """Итог наката: какие файлы применены."""

    model_config = ConfigDict(frozen=True)

    files: Sequence[str]


class SchemaUpgrade:
    """Применяет schema/*.sql одного пакета в базу ix.

    Пакет отдаёт свой каталог схемы и, если его таблицы ссылаются на ядро,
    просит проверить ядро: без него DDL упал бы на первом references, и по
    сообщению сервера было бы непонятно, что делать. База, схема и таймауты
    сессии приходят секцией IxDatabase приложения.
    """

    SUFFIX: ClassVar[str] = "*.sql"
    MISSING_CORE: ClassVar[str] = (
        "upgrade: core table {schema}.{table} is missing in the database; "
        "apply the core first: boba-ix-core upgrade --config <config>"
    )

    def __init__(self, schema_dir: Path, *, requires_core: bool = True) -> None:
        self._schema_dir = schema_dir
        self._requires_core = requires_core

    async def run(self, database: IxDatabase) -> UpgradeReport:
        files = self._files()

        try:
            async with IxPool.session(database) as conn:
                if self._requires_core:
                    await self._validate_core_layer_exists(conn, database.db_schema)

                for path in files:
                    logger.info("applying %s", path.name)
                    text = path.read_text(encoding="utf-8")
                    await conn.execute(SchemaName.render(text, database.db_schema))

                await self._check_declarations(conn, database.db_schema)
                await self._check_index_tables(conn, database.db_schema)

        except IxDatabaseError as exc:
            msg = f"upgrade {self._schema_dir}: {exc}"
            raise SchemaUpgradeError(msg) from exc
        except psycopg.Error as exc:
            msg = f"upgrade {self._schema_dir}: applying schema failed: {exc}"
            raise SchemaUpgradeError(msg) from exc
        except AspectDeclarationError as exc:
            msg = f"upgrade {self._schema_dir}: aspect declaration rejected: {exc}"
            raise SchemaUpgradeError(msg) from exc
        except IndexTableError as exc:
            msg = f"upgrade {self._schema_dir}: index table rejected: {exc}"
            raise SchemaUpgradeError(msg) from exc

        return UpgradeReport(files=[path.name for path in files])

    def _files(self) -> list[Path]:
        if not self._schema_dir.is_dir():
            msg = f"upgrade: schema directory {self._schema_dir} does not exist"
            raise SchemaUpgradeError(msg)

        files = sorted(self._schema_dir.glob(self.SUFFIX))
        if not files:
            msg = f"upgrade: no {self.SUFFIX} files under {self._schema_dir}"
            raise SchemaUpgradeError(msg)

        return files

    @staticmethod
    async def _check_declarations(
        conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> None:
        if not await SchemaName.exists(conn, db_schema, CoreTable.SURFACE_ASPECT):
            return

        declarations = await AspectDeclarations.all(conn, db_schema)
        await AspectContract.check(conn, db_schema, declarations)
        logger.info("aspect declarations verified: %d", len(declarations))

    @staticmethod
    async def _check_index_tables(
        conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> None:
        if not await SchemaName.exists(conn, db_schema, CoreTable.INDEX_TABLE):
            return

        tables = await IndexTables.all(conn, db_schema)
        await IndexTables.check(conn, db_schema, tables)
        logger.info("index tables verified: %d", len(tables))

    @classmethod
    async def _validate_core_layer_exists(
        cls, conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> None:
        if not await SchemaName.exists(conn, db_schema, CoreTable.NODE):
            raise SchemaUpgradeError(
                cls.MISSING_CORE.format(schema=db_schema, table=CoreTable.NODE)
            )

        if not await SchemaName.exists(conn, db_schema, CoreTable.EDGE):
            raise SchemaUpgradeError(
                cls.MISSING_CORE.format(schema=db_schema, table=CoreTable.EDGE)
            )
