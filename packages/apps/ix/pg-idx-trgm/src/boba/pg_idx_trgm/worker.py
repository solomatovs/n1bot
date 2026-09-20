"""Воркер индексатора pg_trgm: 10_upsert.sql пачками до пустого результата, затем
20_prune.sql. Источник аспектов собирается при старте из объявлений
{schema}.surface_aspect по классам из конфига и подставляется в файлы run/
вместо `{sources}`.

Ошибки:
IndexerWorkerError — база ix недоступна или ответ шага не того вида, что ожидался.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field

from boba.config import ConfigError, bind_section
from boba.pg_ix_core.aspects import AspectClass, AspectDeclarations, AspectSources
from boba.pg_ix_core.schema_name import SchemaName, StorageSchema
from boba.pg_ix_core.upgrade import (
    SchemaUpgrade,
    SchemaUpgradeError,
    UpgradeConfig,
)

logger = logging.getLogger("pg-idx-trgm")


class IndexerWorkerError(Exception):
    """Ошибка цикла воркера: база или контракт файлов пакета."""


class SqlFile(StrEnum):
    UPSERT = "10_upsert.sql"
    PRUNE = "20_prune.sql"


class Part(StrEnum):
    """Плейсхолдеры файлов run/, которые заполняет воркер."""

    SOURCES = "sources"


class WorkerConfig(StorageSchema):
    dsn: str
    classes: Sequence[AspectClass]
    batch: int = Field(gt=0, default=500)
    lock_timeout: str = "2s"
    statement_timeout: str = "60s"


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

    def run(self) -> CycleReport:
        try:
            with psycopg.connect(
                self._cfg.dsn, autocommit=True, application_name="pg-idx-trgm"
            ) as conn:
                conn.execute(
                    sql.SQL("set lock_timeout = {}").format(
                        sql.Literal(self._cfg.lock_timeout)
                    )
                )
                conn.execute(
                    sql.SQL("set statement_timeout = {}").format(
                        sql.Literal(self._cfg.statement_timeout)
                    )
                )

                sql_files = PackageSql(
                    self._dir, self._cfg.db_schema, self._parts(conn)
                )

                rounds = 0
                applied = 0
                while True:
                    step = self._upsert(conn, sql_files)
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

                pruned = self._prune(conn, sql_files)

                return CycleReport(rounds=rounds, applied=applied, pruned=pruned)
        except psycopg.Error as exc:
            raise IndexerWorkerError(f"ix database {self._cfg.dsn}: {exc}") from exc

    def _parts(self, conn: psycopg.Connection) -> dict[str, sql.Composable]:
        declarations = AspectDeclarations.of_classes(
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

    def _upsert(self, conn: psycopg.Connection, sql_files: PackageSql) -> StepResult:
        record = conn.execute(
            sql_files.load(SqlFile.UPSERT), {"batch": self._cfg.batch}
        ).fetchone()
        if record is None:
            raise IndexerWorkerError("upsert: expected one summary row, got none")

        return StepResult(planned=int(record[1]), applied=int(record[2]))

    def _prune(self, conn: psycopg.Connection, sql_files: PackageSql) -> int:
        record = conn.execute(sql_files.load(SqlFile.PRUNE)).fetchone()
        if record is None:
            raise IndexerWorkerError("prune: expected one summary row, got none")

        return int(record[1])


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или отработать цикл."""

    UPGRADE = "upgrade"
    RUN = "run"


class Cli:
    """Команда и путь к конфигу; настройки берутся из секции [ix.idx_trgm]."""

    SECTION: ClassVar[str] = "ix.idx_trgm"

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> tuple[Command, Path]:
        parser = argparse.ArgumentParser(
            prog="boba-pg-idx-trgm",
            description=(
                "Индексатор триграмм и префиксов pg_idx_trgm: схема пакета "
                "и цикл upsert/prune."
            ),
        )
        parser.add_argument(
            "command",
            type=Command,
            choices=list(Command),
            help=(
                "upgrade — накатить схему пакета в базу ix (идемпотентно, ядро "
                "должно быть уже накачено пакетом pg-ix-core); run — рабочий цикл."
            ),
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help=(
                "Путь к файлу конфига приложения (toml). Все настройки, включая "
                "строку подключения к базе ix, берутся из секции [ix.idx_trgm]."
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
            upgrade = bind_section(config_path, Cli.SECTION, UpgradeConfig)
            report = SchemaUpgrade(package_dir / "schema").run(upgrade)
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = bind_section(config_path, Cli.SECTION, WorkerConfig)
        report = IndexerWorker(cfg, package_dir / "run").run()
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
