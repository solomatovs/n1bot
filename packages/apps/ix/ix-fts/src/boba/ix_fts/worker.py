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
from pydantic import BaseModel, Field

from boba.config import ConfigError, bind_section
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.aspects import AspectClass, AspectDeclarations, AspectSources
from boba.ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError

logger = logging.getLogger("ix-fts")


class IndexerWorkerError(Exception):
    """Ошибка цикла воркера: база или контракт файлов пакета."""


class SqlFile(StrEnum):
    UPSERT = "10_upsert.sql"
    PRUNE = "20_prune.sql"


class Part(StrEnum):
    """Плейсхолдеры файлов run/, которые заполняет воркер."""

    SOURCES = "sources"
    WEIGHTS = "weights"


class FtsWeight(StrEnum):
    """Вес tsvector: A самый тяжёлый, D по умолчанию для аспекта без веса."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"


class WorkerConfig(IxDatabase):
    classes: Sequence[AspectClass]
    weights: Mapping[str, FtsWeight]
    batch: int = Field(gt=0, default=500)


class StepResult(BaseModel):
    planned: int
    applied: int


class CycleReport(BaseModel):
    rounds: int
    applied: int
    pruned: int


class FtsWeights:
    """Веса аспектов из конфига как список values для `{weights}`: аспект как
    значение aspect_e, вес текстом; аспект без строки получает D в запросе."""

    @classmethod
    def values(cls, weights: Mapping[str, FtsWeight], db_schema: str) -> sql.Composed:
        schema = sql.Identifier(db_schema)

        if not weights:
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
        for aspect, weight in sorted(weights.items()):
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


class IndexerWorker:
    """Цикл индексатора: одна сессия к ix."""

    def __init__(self, cfg: WorkerConfig, package_dir: Path) -> None:
        self._cfg = cfg
        self._dir = package_dir

    async def run(self) -> CycleReport:
        try:
            async with IxPool.session(self._cfg) as conn:
                parts = await self._parts(conn)
                names: dict[str, sql.Composable] = {
                    "schema": sql.Identifier(self._cfg.db_schema),
                    **parts,
                }

                rounds = 0
                applied = 0
                while True:
                    step = await self._upsert(conn, names)
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

                pruned = await self._prune(conn, names)

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
            str(Part.WEIGHTS): FtsWeights.values(
                self._cfg.weights, self._cfg.db_schema
            ),
        }

    async def _upsert(
        self, conn: psycopg.AsyncConnection[Any], names: Mapping[str, sql.Composable]
    ) -> StepResult:
        query = (
            PgQueryBuilder(**names)
            .read(self._dir / SqlFile.UPSERT, batch=self._cfg.batch)
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        record = await cur.fetchone()
        if record is None:
            raise IndexerWorkerError("upsert: expected one summary row, got none")

        return StepResult(planned=int(record[1]), applied=int(record[2]))

    async def _prune(
        self, conn: psycopg.AsyncConnection[Any], names: Mapping[str, sql.Composable]
    ) -> int:
        query = PgQueryBuilder(**names).read(self._dir / SqlFile.PRUNE).build()
        cur = await conn.execute(query.text, query.params)
        record = await cur.fetchone()
        if record is None:
            raise IndexerWorkerError("prune: expected one summary row, got none")

        return int(record[1])


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или отработать цикл."""

    UPGRADE = "upgrade"
    RUN = "run"


class Cli:
    """Команда и путь к конфигу; настройки берутся из секции [ix.fts]."""

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> tuple[Command, Path]:
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
            type=Path,
            help=(
                "Путь к файлу конфига приложения (toml). Все настройки, включая "
                "профиль подключения к базе ix, берутся из секции [ix.fts]."
            ),
        )
        args = parser.parse_args(argv)

        return args.command, args.config


def main() -> None:
    section = "ix.fts"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    package_dir = Path(__file__).resolve().parent
    try:
        command, config_path = Cli.parse()

        if command is Command.UPGRADE:
            database = bind_section(config_path, section, IxDatabase)
            upgrade = SchemaUpgrade(package_dir / "schema")
            report = asyncio.run(upgrade.run(database))
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = bind_section(config_path, section, WorkerConfig)
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
