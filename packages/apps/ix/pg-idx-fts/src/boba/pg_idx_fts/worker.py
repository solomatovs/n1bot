"""Воркер индексатора pg_fts: 10_upsert.sql пачками до пустого результата, затем
20_prune.sql.

Ошибки:
IndexerWorkerError — база ix недоступна или ответ шага не того вида, что ожидался.
"""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field

from boba.config import ConfigError, bind_section
from boba.pg_ix_core.schema_name import SchemaName, StorageSchema
from boba.pg_ix_core.upgrade import (
    SchemaUpgrade,
    SchemaUpgradeError,
    UpgradeConfig,
)

logger = logging.getLogger("pg-idx-fts")


class IndexerWorkerError(Exception):
    """Ошибка цикла воркера: база или контракт файлов пакета."""


class SqlFile(StrEnum):
    UPSERT = "10_upsert.sql"
    PRUNE = "20_prune.sql"


class WorkerConfig(StorageSchema):
    dsn: str
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
    """Файлы цикла из каталога run/ пакета; параметры именованные, в стиле psycopg.

    У шага может быть вариант с суффиксом __<имя> и заголовком `-- @requires
    <таблица>`: он берётся, когда все перечисленные таблицы есть в базе
    (to_regclass), иначе берётся базовый файл. Так индекс подхватывает описания от
    pg-llm-describer, если тот установлен, и работает без него.
    """

    REQUIRES = re.compile(r"^-- @requires\s+(.+)$", re.M)

    def __init__(self, package_dir: Path, db_schema: str) -> None:
        self._dir = package_dir
        self._db_schema = db_schema
        self._present: dict[str, bool] = {}

    def load(self, name: SqlFile, conn: psycopg.Connection) -> sql.Composed:
        stem = Path(name).stem
        chosen = self._dir / name
        for variant in sorted(self._dir.glob(f"{stem}__*.sql")):
            text = variant.read_text(encoding="utf-8")
            required = [
                t.strip()
                for m in self.REQUIRES.finditer(text)
                for t in m.group(1).split()
            ]
            if required and all(self._exists(conn, t) for t in required):
                chosen = variant
        return SchemaName.render(chosen.read_text(encoding="utf-8"), self._db_schema)

    def _exists(self, conn: psycopg.Connection, table: str) -> bool:
        if table not in self._present:
            self._present[table] = SchemaName.exists(conn, self._db_schema, table)
        return self._present[table]


class IndexerWorker:
    """Цикл индексатора: одна сессия к ix."""

    def __init__(self, cfg: WorkerConfig, sql_files: PackageSql) -> None:
        self._cfg = cfg
        self._sql = sql_files

    def run(self) -> CycleReport:
        try:
            with psycopg.connect(
                self._cfg.dsn, autocommit=True, application_name="pg-idx-fts"
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
                rounds = 0
                applied = 0
                while True:
                    step = self._upsert(conn)
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
                pruned = self._prune(conn)
                return CycleReport(rounds=rounds, applied=applied, pruned=pruned)
        except psycopg.Error as exc:
            raise IndexerWorkerError(f"ix database {self._cfg.dsn}: {exc}") from exc

    def _upsert(self, conn: psycopg.Connection) -> StepResult:
        record = conn.execute(
            self._sql.load(SqlFile.UPSERT, conn), {"batch": self._cfg.batch}
        ).fetchone()
        if record is None:
            raise IndexerWorkerError("upsert: expected one summary row, got none")
        return StepResult(planned=int(record[1]), applied=int(record[2]))

    def _prune(self, conn: psycopg.Connection) -> int:
        record = conn.execute(self._sql.load(SqlFile.PRUNE, conn)).fetchone()
        if record is None:
            raise IndexerWorkerError("prune: expected one summary row, got none")
        return int(record[1])


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или отработать цикл."""

    UPGRADE = "upgrade"
    RUN = "run"


class Cli:
    """Команда и путь к конфигу; настройки берутся из секции [ix.idx_fts]."""

    SECTION: ClassVar[str] = "ix.idx_fts"

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> tuple[Command, Path]:
        parser = argparse.ArgumentParser(
            prog="boba-pg-idx-fts",
            description=(
                "Индексатор полнотекстового поиска pg_idx_fts: схема пакета "
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
                "строку подключения к базе ix, берутся из секции [ix.idx_fts]."
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
        report = IndexerWorker(
            cfg, PackageSql(package_dir / "run", cfg.db_schema)
        ).run()
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
