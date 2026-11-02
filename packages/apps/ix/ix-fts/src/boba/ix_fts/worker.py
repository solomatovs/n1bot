"""Воркер индексатора pg_fts: 10_upsert.sql пачками до пустого результата, затем
20_prune.sql. Источник аспектов собирается при старте из объявлений
{schema}.surface_aspect по классам из конфига и подставляется в файлы run/
вместо `{sources}`; веса аспектов из конфига подставляются вместо `{weights}`.

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
from typing import Any

import psycopg
from psycopg import sql

from boba.cancellation import ProcessStop
from boba.config import ConfigError
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.database import IxConfigFile, IxDatabase
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError
from boba.ix_core.worker import (
    CycleReport,
    IndexerWorkerError,
    SqlIndexCycle,
    StepResult,
)
from boba.ix_core.worker import WorkerConfig as SqlWorkerConfig

__all__ = [
    "CycleReport",
    "FtsWeight",
    "IndexerWorker",
    "IndexerWorkerError",
    "StepResult",
    "WorkerConfig",
    "cli",
    "main",
    "parse_args",
    "work",
]

logger = logging.getLogger("ix-fts")


class FtsWeight(StrEnum):
    """Вес tsvector: A самый тяжёлый, D по умолчанию для аспекта без веса."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"


class WorkerConfig(SqlWorkerConfig):
    weights: Mapping[str, FtsWeight]


class IndexerWorker(SqlIndexCycle[WorkerConfig]):
    """Общий SQL-цикл с весами аспектов полнотекстового индекса."""

    def __init__(self, cfg: WorkerConfig, package_dir: Path) -> None:
        super().__init__(cfg, package_dir, logger)

    def _weights(self) -> sql.Composed:
        """Веса аспектов из конфига как список values для `{weights}`: аспект как
        значение aspect_e, вес текстом; аспект без строки получает D в запросе."""
        schema = sql.Identifier(self._cfg.db_schema)

        if not self._cfg.weights:
            return (
                PgQueryBuilder()
                .add(
                    "select null::{schema}.aspect_e, null::text where false",
                    schema=schema,
                )
                .build()
                .text
            )

        rows: list[sql.Composed] = []
        for aspect, weight in sorted(self._cfg.weights.items()):
            rows.append(
                PgQueryBuilder()
                .add(
                    "({aspect}::{schema}.aspect_e, {weight})",
                    schema=schema,
                    aspect=sql.Literal(aspect),
                    weight=sql.Literal(str(weight)),
                )
                .build()
                .text
            )

        return sql.SQL("values ") + sql.SQL(", ").join(rows)

    async def _parts(
        self, conn: psycopg.AsyncConnection[Any]
    ) -> dict[str, sql.Composable]:
        parts = await super()._parts(conn)
        parts["weights"] = self._weights()
        return parts


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или отработать цикл."""

    UPGRADE = "upgrade"
    RUN = "run"


def parse_args(argv: Sequence[str] | None = None) -> tuple[Command, Sequence[Path]]:
    """Команда и файлы конфига: главный и наложения поверх него; настройки
    берутся из секции [ix.fts]."""
    parser = argparse.ArgumentParser(
        prog="boba-ix-fts",
        description=(
            "Индексатор полнотекстового поиска ix_fts: схема пакета "
            "и цикл upsert/prune."
        ),
    )
    parser.add_argument(
        "command",
        type=Command,
        default=Command.RUN,
        choices=list(Command),
        help=(
            "upgrade — накатить схему пакета в базу ix (идемпотентно, ядро "
            "должно быть уже накачено пакетом ix-core); run — рабочий цикл."
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        action="append",
        type=Path,
        help=(
            "Файл конфига приложения (toml); повтор кладёт наложение поверх. "
            "Все настройки, включая профиль подключения к базе ix, берутся "
            "из секции [ix.fts]."
        ),
    )
    args = parser.parse_args(argv)

    return args.command, args.config


async def work() -> None:
    section = "ix.fts"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    package_dir = Path(__file__).resolve().parent
    try:
        command, config_paths = parse_args()
        config = IxConfigFile(config_paths)
        config.enter_kerberos()

        if command is Command.UPGRADE:
            database = config.section(section, IxDatabase)
            upgrade = SchemaUpgrade(package_dir / "schema")
            report = await upgrade.run(database)
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = config.section(section, WorkerConfig)
        worker = IndexerWorker(cfg, package_dir / "run")
        report = await worker.run()
        logger.info(
            "done: rounds=%d applied=%d pruned=%d",
            report.rounds,
            report.applied,
            report.pruned,
        )
    except (ConfigError, SchemaUpgradeError, IndexerWorkerError) as exc:
        raise SystemExit(str(exc)) from exc


async def main() -> None:
    """Работа приложения под остановкой процесса: SIGTERM и SIGINT отменяют
    её, незакрытое закрывается, процесс выходит сам."""
    await ProcessStop().run(work())


def cli() -> None:
    """Точка входа консольного скрипта: единственный asyncio.run на процесс."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
