"""Воркер индексатора pg_trgm: 10_upsert.sql пачками до пустого результата, затем
20_prune.sql. Источник аспектов собирается при старте из объявлений
{schema}.surface_aspect по классам из конфига и подставляется в файлы run/
вместо `{sources}`.

Ошибки:
IndexerWorkerError — база ix недоступна или ответ шага не того вида, что ожидался.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field

from boba.config import ConfigError, bind_section
from boba.ix_core.aspects import AspectClass, AspectDeclarations, AspectSources
from boba.ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.ix_core.schema_name import SchemaName
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError

logger = logging.getLogger("ix-trgm")


class IndexerWorkerError(Exception):
    """Ошибка цикла воркера: база или контракт файлов пакета."""


class SqlFile(StrEnum):
    UPSERT = "10_upsert.sql"
    PRUNE = "20_prune.sql"


class Part(StrEnum):
    """Плейсхолдеры файлов run/, которые заполняет воркер."""

    SOURCES = "sources"


class WorkerConfig(IxDatabase):
    classes: Sequence[AspectClass]
    batch: int = Field(gt=0, default=500)


class StepResult(BaseModel):
    planned: int
    applied: int


class CycleReport(BaseModel):
    rounds: int
    applied: int
    pruned: int


class PackageSql:
    """Файлы цикла из каталога run/ пакета; параметры именованные, в стиле psycopg,
    плейсхолдеры файла заполняются частями, собранными воркером при старте."""

    def __init__(
        self, package_dir: Path, db_schema: str, parts: Mapping[str, sql.Composable]
    ) -> None:
        self._dir = package_dir
        self._db_schema = db_schema
        self._parts = dict(parts)

    def load(self, name: SqlFile) -> sql.Composed:
        text = (self._dir / name).read_text(encoding="utf-8")

        return SchemaName.render(text, self._db_schema, **self._parts)


class IndexerWorker:
    """Цикл индексатора: одна сессия к ix."""

    def __init__(self, cfg: WorkerConfig, package_dir: Path) -> None:
        self._cfg = cfg
        self._dir = package_dir

    async def run(self) -> CycleReport:
        try:
            async with IxPool.session(self._cfg) as conn:
                parts = await self._parts(conn)
                sql_files = PackageSql(self._dir, self._cfg.db_schema, parts)

                rounds = 0
                applied = 0
                while True:
                    step = await self._upsert(conn, sql_files)
                    rounds += 1
                    applied += step.applied
                    logger.info(
                        "round %d: planned=%d applied=%d",
                        rounds,
                        step.planned,
                        step.applied,
                    )
                    if step.applied == 0:
                        break

                pruned = await self._prune(conn, sql_files)

                return CycleReport(rounds=rounds, applied=applied, pruned=pruned)
        except IxDatabaseError as exc:
            raise IndexerWorkerError(str(exc)) from exc
        except psycopg.Error as exc:
            msg = f"ix database {self._cfg.postgres.where()}: {exc}"
            raise IndexerWorkerError(msg) from exc

    async def _parts(
        self, conn: psycopg.AsyncConnection[Any]
    ) -> dict[str, sql.Composable]:
        declarations = await AspectDeclarations.of_classes(
            conn, self._cfg.db_schema, self._cfg.classes
        )
        logger.info(
            "aspect sources: %d declarations for classes %s",
            len(declarations),
            ", ".join(self._cfg.classes),
        )

        return {
            str(Part.SOURCES): AspectSources.union(declarations, self._cfg.db_schema),
        }

    async def _upsert(
        self, conn: psycopg.AsyncConnection[Any], sql_files: PackageSql
    ) -> StepResult:
        cur = await conn.execute(
            sql_files.load(SqlFile.UPSERT), {"batch": self._cfg.batch}
        )
        record = await cur.fetchone()
        if record is None:
            raise IndexerWorkerError("upsert: expected one summary row, got none")

        return StepResult(planned=int(record[1]), applied=int(record[2]))

    async def _prune(
        self, conn: psycopg.AsyncConnection[Any], sql_files: PackageSql
    ) -> int:
        cur = await conn.execute(sql_files.load(SqlFile.PRUNE))
        record = await cur.fetchone()
        if record is None:
            raise IndexerWorkerError("prune: expected one summary row, got none")

        return int(record[1])


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или отработать цикл."""

    UPGRADE = "upgrade"
    RUN = "run"


class Cli:
    """Команда и путь к конфигу; настройки берутся из секции [ix.trgm]."""

    SECTION: ClassVar[str] = "ix.trgm"

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> tuple[Command, Path]:
        parser = argparse.ArgumentParser(
            prog="boba-ix-trgm",
            description=(
                "Индексатор триграмм и префиксов ix_trgm: схема пакета "
                "и цикл upsert/prune."
            ),
        )
        parser.add_argument(
            "command",
            type=Command,
            choices=list(Command),
            help=(
                "upgrade — накатить схему пакета в базу ix (идемпотентно, ядро "
                "должно быть уже накачено пакетом ix-core); run — рабочий цикл."
            ),
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help=(
                "Путь к файлу конфига приложения (toml). Все настройки, включая "
                "профиль подключения к базе ix, берутся из секции [ix.trgm]."
            ),
        )
        args = parser.parse_args(argv)

        return args.command, args.config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    package_dir = Path(__file__).resolve().parent
    try:
        command, config_path = Cli.parse()

        if command is Command.UPGRADE:
            database = bind_section(config_path, Cli.SECTION, IxDatabase)
            upgrade = SchemaUpgrade(package_dir / "schema")
            report = asyncio.run(upgrade.run(database))
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = bind_section(config_path, Cli.SECTION, WorkerConfig)
        worker = IndexerWorker(cfg, package_dir / "run")
        report = asyncio.run(worker.run())
        logger.info(
            "done: rounds=%d applied=%d pruned=%d",
            report.rounds,
            report.applied,
            report.pruned,
        )
    except (ConfigError, SchemaUpgradeError, IndexerWorkerError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
