"""Общий SQL-цикл FTS/TRGM: upsert до пустой пачки, затем prune.

Конкретный индексатор владеет SQL-файлами и дополнительными подстановками.
Сессия PostgreSQL, чтение реестра аспектов и отчёт цикла общие.

Ошибки:
IndexerWorkerError — отказ PostgreSQL или отсутствие итоговой строки SQL-шага.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, TypeVar

import psycopg
from psycopg import sql
from pydantic import Field

from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.aspects import AspectClass
from boba.ix_core.database import IxDatabase
from boba.ix_core.registry import IxRegistry


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


@dataclass(frozen=True, kw_only=True)
class StepResult:
    planned: int
    applied: int


@dataclass(frozen=True, kw_only=True)
class CycleReport:
    rounds: int
    applied: int
    pruned: int


ConfigT = TypeVar("ConfigT", bound=WorkerConfig)


class SqlIndexCycle(Generic[ConfigT]):
    """Цикл индексатора: одна сессия к ix."""

    def __init__(self, cfg: ConfigT, package_dir: Path, logger: logging.Logger) -> None:
        self._cfg = cfg
        self._logger = logger
        self._dir = package_dir
        self._registry = IxRegistry(cfg.db_schema)

    async def run(self) -> CycleReport:
        try:
            async with await AsyncPostgresPool.dedicated(self._cfg.postgres) as conn:
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
                    self._logger.info(
                        "round %d: planned=%d applied=%d",
                        rounds,
                        step.planned,
                        step.applied,
                    )
                    if step.applied == 0:
                        break

                pruned = await self._prune(conn, names)

                return CycleReport(rounds=rounds, applied=applied, pruned=pruned)
        except PostgresError as exc:
            raise IndexerWorkerError(str(exc)) from exc
        except psycopg.Error as exc:
            msg = f"ix database {self._cfg.postgres.where()}: {exc}"
            raise IndexerWorkerError(msg) from exc

    async def _parts(
        self, conn: psycopg.AsyncConnection[Any]
    ) -> dict[str, sql.Composable]:
        declarations = await self._registry.read_declarations(conn, self._cfg.classes)
        self._logger.info(
            "aspect sources: %d declarations for classes %s",
            len(declarations),
            ", ".join(self._cfg.classes),
        )

        return {
            str(Part.SOURCES): self._registry.union_sources(declarations),
        }

    async def _upsert(
        self, conn: psycopg.AsyncConnection[Any], names: Mapping[str, sql.Composable]
    ) -> StepResult:
        query = (
            PgQueryBuilder(**names)
            .from_file(self._dir / SqlFile.UPSERT, batch=self._cfg.batch)
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
        query = PgQueryBuilder(**names).from_file(self._dir / SqlFile.PRUNE).build()
        cur = await conn.execute(query.text, query.params)
        record = await cur.fetchone()
        if record is None:
            raise IndexerWorkerError("prune: expected one summary row, got none")

        return int(record[1])
