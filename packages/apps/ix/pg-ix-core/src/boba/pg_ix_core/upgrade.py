"""Накат схемы в базу ix: ядро пакетом pg-ix-core, свои таблицы каждым пакетом.

Файлы schema/*.sql применяются по порядку имён, каждый файл одной командой
psycopg в autocommit; схема подставляется в `{schema}` перед отправкой. Файл
самодостаточен: значения enum, добавленные `alter type ... add value`, нельзя
использовать в своей же транзакции, поэтому пакет кладёт их отдельным файлом,
а их использование следующим. Файлы идемпотентны (`if not exists`,
`on conflict do nothing`), повторный накат безопасен.

После файлов проверяются все объявления аспектов {schema}.surface_aspect:
тело каждого выполняется с limit 0 и сверяется с контрактом, так что опечатка
владельца поверхности валит его же накат, а не прогон потребителя.

Ошибки:
SchemaUpgradeError — база недоступна, каталога схемы нет, сервер отклонил DDL,
    ядро ix отсутствует там, где пакет на него опирается, или объявление
    аспекта нарушает контракт.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from boba.pg_ix_core.aspects import (
    AspectContract,
    AspectDeclarationError,
    AspectDeclarations,
)
from boba.pg_ix_core.schema_name import SchemaName, StorageSchema

__all__ = [
    "CoreTable",
    "SchemaUpgrade",
    "SchemaUpgradeError",
    "UpgradeConfig",
    "UpgradeReport",
]

logger = logging.getLogger("ix-upgrade")


class SchemaUpgradeError(Exception):
    """Схему не удалось применить."""


class CoreTable:
    """Таблица ядра, наличием которой пакет проверяет, что ядро уже накачено."""

    NODE: ClassVar[str] = "node"
    SURFACE_ASPECT: ClassVar[str] = "surface_aspect"


class UpgradeConfig(StorageSchema):
    """Секция конфига команды upgrade: куда накатывать и в какую схему."""

    dsn: str = Field(min_length=1)
    statement_timeout: str = "60s"


class UpgradeReport(BaseModel):
    """Итог наката: какие файлы применены."""

    model_config = ConfigDict(frozen=True)

    files: Sequence[str]


class SchemaUpgrade:
    """Применяет schema/*.sql одного пакета в базу ix.

    Пакет отдаёт свой каталог схемы и, если его таблицы ссылаются на ядро,
    просит проверить ядро: без него DDL упал бы на первом references, и по
    сообщению сервера было бы непонятно, что делать.
    """

    SUFFIX: ClassVar[str] = "*.sql"

    def __init__(self, schema_dir: Path, *, requires_core: bool = True) -> None:
        self._schema_dir = schema_dir
        self._requires_core = requires_core

    def run(self, cfg: UpgradeConfig) -> UpgradeReport:
        files = self._files()

        try:
            with psycopg.connect(cfg.dsn, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("set statement_timeout = {}").format(
                        sql.Literal(cfg.statement_timeout)
                    )
                )
                if self._requires_core:
                    self._require_core(conn, cfg.db_schema)

                for path in files:
                    logger.info("applying %s", path.name)
                    text = path.read_text(encoding="utf-8")
                    conn.execute(SchemaName.render(text, cfg.db_schema))

                self._check_declarations(conn, cfg.db_schema)

        except psycopg.Error as exc:
            msg = f"upgrade {self._schema_dir}: applying schema failed: {exc}"
            raise SchemaUpgradeError(msg) from exc
        except AspectDeclarationError as exc:
            msg = f"upgrade {self._schema_dir}: aspect declaration rejected: {exc}"
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
    def _check_declarations(conn: psycopg.Connection, db_schema: str) -> None:
        if not SchemaName.exists(conn, db_schema, CoreTable.SURFACE_ASPECT):
            return

        declarations = AspectDeclarations.all(conn, db_schema)
        AspectContract.check(conn, db_schema, declarations)
        logger.info("aspect declarations verified: %d", len(declarations))

    @staticmethod
    def _require_core(conn: psycopg.Connection, db_schema: str) -> None:
        if SchemaName.exists(conn, db_schema, CoreTable.NODE):
            return

        msg = (
            f"upgrade: core table {db_schema}.{CoreTable.NODE} is missing in the "
            "database; apply the core first: boba-ix-core upgrade --config <config>"
        )
        raise SchemaUpgradeError(msg)
