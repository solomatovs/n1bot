"""Воркер векторного индексатора: очередь из ix, резка на чанки, эмбеддинг провайдером
проекта, запись набора чанков аспекта.

Один цикл: 10_queue.sql пачкой, чанки, векторы и 20_write.sql на каждый аспект делает
AspectEmbedding из embedding.py (библиотечная часть пакета, её же зовут индексаторы
других происхождений), затем 90_unlock.sql, и так до пустой очереди; в конце
30_prune.sql. Источник аспектов собирается при старте
из объявлений {schema}.surface_aspect по классам из конфига и подставляется вместо
`{sources}`; на каждую объявленную пару surface + aspect воркер ставит частичный HNSW
файлом 05_index.sql.

Ошибки:
VectorWorkerError — база ix недоступна или ответ шага не того вида, что ожидался.
AspectEmbeddingError — модель, чанкер или провайдер эмбеддингов (embedding.py).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from boba.config import ConfigError, bind_section
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.aspects import AspectClass, SurfaceAspect
from boba.ix_core.database import IxDatabase, enter_kerberos
from boba.ix_core.registry import IxRegistry
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError
from boba.ix_vector.embedding import (
    AspectEmbedding,
    AspectEmbeddingError,
    AspectText,
    EmbeddingParams,
)

logger = logging.getLogger("ix-vector")


class VectorWorkerError(Exception):
    """Ошибка цикла воркера: база, провайдер или контракт файлов пакета."""


class SqlFile(StrEnum):
    INDEX = "05_index.sql"
    QUEUE = "10_queue.sql"
    PRUNE = "30_prune.sql"
    UNLOCK = "90_unlock.sql"


class Part(StrEnum):
    """Плейсхолдеры файлов run/, которые заполняет воркер."""

    SOURCES = "sources"
    INDEX_NAME = "index_name"
    SURFACE = "surface"
    ASPECT = "aspect"


class WorkerConfig(IxDatabase, EmbeddingParams):
    classes: Sequence[AspectClass]


@dataclass(frozen=True, kw_only=True)
class CycleReport:
    rounds: int
    written: int
    pruned: int


class VectorWorker:
    """Цикл индексатора: одна сессия к ix, один эмбеддер проекта."""

    def __init__(self, cfg: WorkerConfig, package_dir: Path) -> None:
        self._cfg = cfg
        self._dir = package_dir
        self._registry = IxRegistry(cfg.db_schema)
        self._embedding = AspectEmbedding(cfg, cfg.db_schema, "ix_emb_e5_1024")

    async def run(self) -> CycleReport:
        try:
            async with await AsyncPostgresPool.dedicated(self._cfg.postgres) as conn:
                declarations = await self._registry.read_declarations(
                    conn, self._cfg.classes
                )
                logger.info(
                    "aspect sources: %d declarations for classes %s",
                    len(declarations),
                    ", ".join(self._cfg.classes),
                )
                names: dict[str, sql.Composable] = {
                    "schema": sql.Identifier(self._cfg.db_schema),
                    str(Part.SOURCES): self._registry.union_sources(declarations),
                }
                await self._ensure_indexes(conn, names, declarations)

                try:
                    rounds, written = await self._upsert_rounds(conn, names)
                finally:
                    await self._unlock(conn, names)

                pruned = await self._prune(conn, names)

                return CycleReport(rounds=rounds, written=written, pruned=pruned)
        except PostgresError as exc:
            raise VectorWorkerError(str(exc)) from exc
        except psycopg.Error as exc:
            msg = f"ix database {self._cfg.postgres.where()}: {exc}"
            raise VectorWorkerError(msg) from exc

    async def _ensure_indexes(
        self,
        conn: psycopg.AsyncConnection[Any],
        names: Mapping[str, sql.Composable],
        declarations: Sequence[SurfaceAspect],
    ) -> None:
        for declaration in declarations:
            name = f"ix_emb_e5_1024__{declaration.surface}_{declaration.aspect}__hnsw"
            query = (
                PgQueryBuilder(**names)
                .read(
                    self._dir / SqlFile.INDEX,
                    index_name=sql.Identifier(name),
                    surface=sql.Literal(declaration.surface),
                    aspect=sql.Literal(declaration.aspect),
                )
                .build()
            )
            await conn.execute(query.text, query.params)

    async def _upsert_rounds(
        self, conn: psycopg.AsyncConnection[Any], names: Mapping[str, sql.Composable]
    ) -> tuple[int, int]:
        rounds = 0
        written = 0
        while True:
            rows = await self._queue(conn, names)
            if not rows:
                break

            chunks = await self._embedding.write(conn, rows)
            await self._unlock(conn, names)
            rounds += 1
            written += len(rows)
            logger.info(
                "round %d: %d aspects written as %d chunks", rounds, len(rows), chunks
            )
        return rounds, written

    async def _queue(
        self, conn: psycopg.AsyncConnection[Any], names: Mapping[str, sql.Composable]
    ) -> list[AspectText]:
        query = (
            PgQueryBuilder(**names)
            .read(self._dir / SqlFile.QUEUE, batch=self._cfg.batch)
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        rows: list[AspectText] = []
        async for node_id, surface, aspect, content, content_hash in cur:
            rows.append(
                AspectText(
                    node_id=node_id,
                    surface=surface,
                    aspect=aspect,
                    content=content,
                    content_hash=content_hash,
                )
            )
        return rows

    async def _unlock(
        self, conn: psycopg.AsyncConnection[Any], names: Mapping[str, sql.Composable]
    ) -> None:
        query = PgQueryBuilder(**names).read(self._dir / SqlFile.UNLOCK).build()
        await conn.execute(query.text, query.params)

    async def _prune(
        self, conn: psycopg.AsyncConnection[Any], names: Mapping[str, sql.Composable]
    ) -> int:
        query = PgQueryBuilder(**names).read(self._dir / SqlFile.PRUNE).build()
        cur = await conn.execute(query.text, query.params)
        record = await cur.fetchone()
        if record is None:
            raise VectorWorkerError("prune: expected one summary row, got none")
        return int(record[1])


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или отработать цикл."""

    UPGRADE = "upgrade"
    RUN = "run"


def parse_args(argv: Sequence[str] | None = None) -> tuple[Command, Path]:
    """Команда и путь к конфигу; настройки берутся из секции [ix.vector]."""
    parser = argparse.ArgumentParser(
        prog="boba-ix-vector",
        description=(
            "Векторный индексатор ix_emb_e5_1024: схема пакета и цикл чанков."
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
            "профиль подключения к базе ix, берутся из секции [ix.vector]."
        ),
    )
    args = parser.parse_args(argv)

    return args.command, args.config


def main() -> None:
    section = "ix.vector"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    package_dir = Path(__file__).resolve().parent
    try:
        command, config_path = parse_args()
        enter_kerberos(config_path)

        if command is Command.UPGRADE:
            database = bind_section(config_path, section, IxDatabase)
            upgrade = SchemaUpgrade(package_dir / "schema")
            report = asyncio.run(upgrade.run(database))
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        cfg = bind_section(config_path, section, WorkerConfig)
        worker = VectorWorker(cfg, package_dir / "run")
        report = asyncio.run(worker.run())
        logger.info(
            "done: rounds=%d written=%d pruned=%d",
            report.rounds,
            report.written,
            report.pruned,
        )
    except (
        AspectEmbeddingError,
        ConfigError,
        SchemaUpgradeError,
        VectorWorkerError,
    ) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
