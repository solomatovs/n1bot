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

logger = logging.getLogger("pg-indexer-trgm")


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
    """Файлы пакета рядом с воркером; $N заменяется на %s, параметры по порядку вхождений.
    Текст отдаётся байтами: psycopg принимает запрос как LiteralString, bytes или sql.SQL."""

    def __init__(self, package_dir: Path) -> None:
        self._dir = package_dir

    def load(self, name: SqlFile) -> tuple[bytes, list[int]]:
        text = (self._dir / name).read_text(encoding="utf-8")
        order = [int(m.group(1)) for m in re.finditer(r"\$(\d+)", text)]
        return re.sub(r"\$(\d+)", "%s", text).encode("utf-8"), order

    @staticmethod
    def bind(order: Sequence[int], values: Sequence[object]) -> list[object]:
        bound: list[object] = []
        for index in order:
            bound.append(values[index - 1])
        return bound


class IndexerWorker:
    """Цикл индексатора: одна сессия к ix."""

    def __init__(self, cfg: WorkerConfig, sql_files: PackageSql) -> None:
        self._cfg = cfg
        self._sql = sql_files

    def run(self) -> CycleReport:
        try:
            with psycopg.connect(
                self._cfg.dsn, autocommit=True, application_name="pg-indexer-trgm"
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
        text, order = self._sql.load(SqlFile.UPSERT)
        record = conn.execute(
            text, PackageSql.bind(order, [self._cfg.batch])
        ).fetchone()
        if record is None:
            raise IndexerWorkerError("upsert: expected one summary row, got none")
        return StepResult(planned=int(record[1]), applied=int(record[2]))

    def _prune(self, conn: psycopg.Connection) -> int:
        text, _ = self._sql.load(SqlFile.PRUNE)
        record = conn.execute(text).fetchone()
        if record is None:
            raise IndexerWorkerError("prune: expected one summary row, got none")
        return int(record[1])


class Cli:
    """Аргументы командной строки в WorkerConfig."""

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> WorkerConfig:
        parser = argparse.ArgumentParser(description="pg-indexer-trgm worker")
        parser.add_argument("--dsn", required=True, help="Строка подключения к базе ix (host=... dbname=... user=... password=...). Единственное место, где задаются креды.")
        parser.add_argument("--batch", type=int, default=500, help="Сколько строк обрабатывать за один шаг upsert. Один шаг это одна транзакция; чем меньше пачка, тем короче замки и тем чаще видны промежуточные результаты.")
        args = parser.parse_args(argv)
        return WorkerConfig(dsn=args.dsn, batch=args.batch)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = Cli.parse()
    report = IndexerWorker(cfg, PackageSql(Path(__file__).resolve().parent)).run()
    logger.info(
        "done: rounds=%d applied=%d pruned=%d",
        report.rounds,
        report.applied,
        report.pruned,
    )


if __name__ == "__main__":
    main()
