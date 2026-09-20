"""Воркер индексатора pg_trgm: 10_upsert.sql пачками до пустого результата, затем 20_prune.sql.

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

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field

logger = logging.getLogger("pg-idx-trgm")


class IndexerWorkerError(Exception):
    """Ошибка цикла воркера: база или контракт файлов пакета."""


class SqlFile(StrEnum):
    UPSERT = "10_upsert.sql"
    PRUNE = "20_prune.sql"


class WorkerConfig(BaseModel):
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
    """Файлы цикла из каталога run/ пакета; параметры именованные, в стиле psycopg. У шага может
    быть вариант с суффиксом __<имя> и заголовком `-- @requires <таблица>`: он берётся, когда
    все перечисленные таблицы существуют в базе (to_regclass), иначе базовый файл. Так индекс
    подхватывает описания от pg-llm-describer, если тот установлен, и работает без него."""

    REQUIRES = re.compile(r"^-- @requires\s+(.+)$", re.M)

    def __init__(self, package_dir: Path) -> None:
        self._dir = package_dir
        self._present: dict[str, bool] = {}

    def load(self, name: SqlFile, conn: psycopg.Connection) -> bytes:
        stem = Path(name).stem
        chosen = self._dir / name
        for variant in sorted(self._dir.glob(f"{stem}__*.sql")):
            text = variant.read_text(encoding="utf-8")
            required = [t.strip() for m in self.REQUIRES.finditer(text) for t in m.group(1).split()]
            if required and all(self._exists(conn, t) for t in required):
                chosen = variant
        return chosen.read_text(encoding="utf-8").encode("utf-8")

    def _exists(self, conn: psycopg.Connection, table: str) -> bool:
        if table not in self._present:
            record = conn.execute("select to_regclass(%s) is not null", (table,)).fetchone()
            self._present[table] = bool(record is not None and record[0])
        return self._present[table]


class IndexerWorker:
    """Цикл индексатора: одна сессия к ix."""

    def __init__(self, cfg: WorkerConfig, sql_files: PackageSql) -> None:
        self._cfg = cfg
        self._sql = sql_files

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
        record = conn.execute(self._sql.load(SqlFile.UPSERT, conn), {"batch": self._cfg.batch}).fetchone()
        if record is None:
            raise IndexerWorkerError("upsert: expected one summary row, got none")
        return StepResult(planned=int(record[1]), applied=int(record[2]))

    def _prune(self, conn: psycopg.Connection) -> int:
        record = conn.execute(self._sql.load(SqlFile.PRUNE, conn)).fetchone()
        if record is None:
            raise IndexerWorkerError("prune: expected one summary row, got none")
        return int(record[1])


class Cli:
    """Аргументы командной строки в WorkerConfig."""

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> WorkerConfig:
        parser = argparse.ArgumentParser(description="pg-idx-trgm worker")
        parser.add_argument("--dsn", required=True, help="Строка подключения к базе ix (host=... dbname=... user=... password=...). Единственное место, где задаются креды.")
        parser.add_argument("--batch", type=int, default=500, help="Сколько строк обрабатывать за один шаг upsert. Один шаг это одна транзакция; чем меньше пачка, тем короче замки и тем чаще видны промежуточные результаты.")
        args = parser.parse_args(argv)
        return WorkerConfig(dsn=args.dsn, batch=args.batch)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = Cli.parse()
    report = IndexerWorker(cfg, PackageSql(Path(__file__).resolve().parent / "run")).run()
    logger.info(
        "done: rounds=%d applied=%d pruned=%d",
        report.rounds,
        report.applied,
        report.pruned,
    )


if __name__ == "__main__":
    main()
