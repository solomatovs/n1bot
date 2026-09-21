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
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import psycopg
from psycopg import sql
from pydantic import BaseModel

from boba.config import ConfigError, bind_section
from boba.ix_core.aspects import (
    AspectClass,
    AspectDeclarations,
    AspectSources,
    SurfaceAspect,
)
from boba.ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.ix_core.schema_name import SchemaName
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


class CycleReport(BaseModel):
    rounds: int
    written: int
    pruned: int


class PackageSql:
    """Файлы цикла из каталога run/ пакета; параметры именованные, в стиле psycopg,
    плейсхолдеры файла заполняются частями, собранными воркером при старте, и
    частями вызова (имя индекса, поверхность, аспект)."""

    def __init__(
        self, package_dir: Path, db_schema: str, parts: Mapping[str, sql.Composable]
    ) -> None:
        self._dir = package_dir
        self._db_schema = db_schema
        self._parts = dict(parts)

    def load(self, name: SqlFile, **extra: sql.Composable) -> sql.Composed:
        text = (self._dir / name).read_text(encoding="utf-8")
        parts = {**self._parts, **extra}

        return SchemaName.render(text, self._db_schema, **parts)


class VectorWorker:
    """Цикл индексатора: одна сессия к ix, один эмбеддер проекта."""

    TABLE: ClassVar[str] = "ix_emb_e5_1024"
    INDEX_NAME: ClassVar[str] = "ix_emb_e5_1024__{surface}_{aspect}__hnsw"

    def __init__(self, cfg: WorkerConfig, package_dir: Path) -> None:
        self._cfg = cfg
        self._dir = package_dir
        self._embedding = AspectEmbedding(cfg, cfg.db_schema, self.TABLE)

    async def run(self) -> CycleReport:
        try:
            async with IxPool.session(self._cfg) as conn:
                declarations = await AspectDeclarations.of_classes(
                    conn, self._cfg.db_schema, self._cfg.classes
                )
                logger.info(
                    "aspect sources: %d declarations for classes %s",
                    len(declarations),
                    ", ".join(self._cfg.classes),
                )
                sql_files = PackageSql(
                    self._dir,
                    self._cfg.db_schema,
                    {
                        str(Part.SOURCES): AspectSources.union(
                            declarations, self._cfg.db_schema
                        )
                    },
                )
                await self._ensure_indexes(conn, sql_files, declarations)

                try:
                    rounds, written = await self._upsert_rounds(conn, sql_files)
                finally:
                    await self._unlock(conn, sql_files)

                pruned = await self._prune(conn, sql_files)

                return CycleReport(rounds=rounds, written=written, pruned=pruned)
        except IxDatabaseError as exc:
            raise VectorWorkerError(str(exc)) from exc
        except psycopg.Error as exc:
            msg = f"ix database {self._cfg.postgres.where()}: {exc}"
            raise VectorWorkerError(msg) from exc

    async def _ensure_indexes(
        self,
        conn: psycopg.AsyncConnection[Any],
        sql_files: PackageSql,
        declarations: Sequence[SurfaceAspect],
    ) -> None:
        for declaration in declarations:
            name = self.INDEX_NAME.format(
                surface=declaration.surface, aspect=declaration.aspect
            )
            await conn.execute(
                sql_files.load(
                    SqlFile.INDEX,
                    index_name=sql.Identifier(name),
                    surface=sql.Literal(declaration.surface),
                    aspect=sql.Literal(declaration.aspect),
                )
            )

    async def _upsert_rounds(
        self, conn: psycopg.AsyncConnection[Any], sql_files: PackageSql
    ) -> tuple[int, int]:
        rounds = 0
        written = 0
        while True:
            rows = await self._queue(conn, sql_files)
            if not rows:
                break

            chunks = await self._embedding.write(conn, rows)
            await self._unlock(conn, sql_files)
            rounds += 1
            written += len(rows)
            logger.info(
                "round %d: %d aspects written as %d chunks", rounds, len(rows), chunks
            )
        return rounds, written

    async def _queue(
        self, conn: psycopg.AsyncConnection[Any], sql_files: PackageSql
    ) -> list[AspectText]:
        cur = await conn.execute(
            sql_files.load(SqlFile.QUEUE), {"batch": self._cfg.batch}
        )
        rows: list[AspectText] = []
        for node_id, surface, aspect, content, content_hash in await cur.fetchall():
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
        self, conn: psycopg.AsyncConnection[Any], sql_files: PackageSql
    ) -> None:
        await conn.execute(sql_files.load(SqlFile.UNLOCK))

    async def _prune(
        self, conn: psycopg.AsyncConnection[Any], sql_files: PackageSql
    ) -> int:
        cur = await conn.execute(sql_files.load(SqlFile.PRUNE))
        record = await cur.fetchone()
        if record is None:
            raise VectorWorkerError("prune: expected one summary row, got none")
        return int(record[1])


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или отработать цикл."""

    UPGRADE = "upgrade"
    RUN = "run"


class Cli:
    """Команда и путь к конфигу; настройки берутся из секции [ix.vector]."""

    SECTION: ClassVar[str] = "ix.vector"

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> tuple[Command, Path]:
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
