"""Воркер векторного индексатора: очередь из ix, резка на чанки, эмбеддинг провайдером
проекта, запись набора чанков аспекта.

Один цикл: 10_queue.sql пачкой, текст каждого аспекта режется токенизатором модели
на окна с перекрытием, embed_documents провайдера boba.llm.embedding по чанкам пачки,
20_write.sql на каждый аспект (весь набор его чанков одним statement'ом), 90_unlock.sql,
и так до пустой очереди; в конце 30_prune.sql. Источник аспектов собирается при старте
из объявлений {schema}.surface_aspect по классам из конфига и подставляется вместо
`{sources}`; на каждую объявленную пару surface + aspect воркер ставит частичный HNSW
файлом 05_index.sql.

Ошибки:
VectorWorkerError — база или провайдер недоступны, ответ не того вида, что ожидался.
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
from tokenizers import Tokenizer

from boba.config import ConfigError, bind_section
from boba.llm.embedding import EmbedderFactory, EmbeddingError, LocalEmbedding
from boba.pg_ix_core.aspects import (
    AspectClass,
    AspectDeclarations,
    AspectSources,
    SurfaceAspect,
)
from boba.pg_ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.pg_ix_core.schema_name import SchemaName
from boba.pg_ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError

logger = logging.getLogger("pg-idx-vector")


class VectorWorkerError(Exception):
    """Ошибка цикла воркера: база, провайдер или контракт файлов пакета."""


class SqlFile(StrEnum):
    INDEX = "05_index.sql"
    QUEUE = "10_queue.sql"
    WRITE = "20_write.sql"
    PRUNE = "30_prune.sql"
    UNLOCK = "90_unlock.sql"


class Part(StrEnum):
    """Плейсхолдеры файлов run/, которые заполняет воркер."""

    SOURCES = "sources"
    INDEX_NAME = "index_name"
    SURFACE = "surface"
    ASPECT = "aspect"


class WorkerConfig(IxDatabase):
    classes: Sequence[AspectClass]
    model: str = "intfloat/multilingual-e5-large"
    cache_dir: str
    dim: int = Field(gt=0, default=1024)
    batch: int = Field(gt=0, default=64)
    chunk_tokens: int = Field(gt=0, default=400)
    chunk_overlap: int = Field(ge=0, default=50)


class QueueRow(BaseModel):
    node_id: int
    surface: str
    aspect: str
    content: str
    content_hash: str


class Chunker:
    """
    Режет текст аспекта на окна по токенам модели с перекрытием. Токенизатор берётся из
    того же кэша fastembed, что и модель, поэтому границы совпадают с тем, что видит
    модель.
    Текст короче окна остаётся одним чанком без перекодирования."""

    TOKENIZER_GLOB = "models--*/snapshots/*/tokenizer.json"

    def __init__(self, cache_dir: str, chunk_tokens: int, overlap: int) -> None:
        if overlap >= chunk_tokens:
            raise VectorWorkerError(
                f"chunking: overlap {overlap} must be smaller than chunk size "
                "{chunk_tokens}"
            )
        found = sorted(Path(cache_dir).glob(self.TOKENIZER_GLOB))
        if not found:
            raise VectorWorkerError(
                f"chunking: no tokenizer.json under {cache_dir}/{self.TOKENIZER_GLOB}"
            )
        self._tokenizer = Tokenizer.from_file(str(found[0]))
        self._size = chunk_tokens
        self._step = chunk_tokens - overlap

    def split(self, text: str) -> list[str]:
        ids = self._tokenizer.encode(text, add_special_tokens=False).ids
        if len(ids) <= self._size:
            return [text]
        chunks: list[str] = []
        start = 0
        while start < len(ids):
            chunks.append(self._tokenizer.decode(ids[start : start + self._size]))
            if start + self._size >= len(ids):
                break
            start += self._step
        return chunks


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

    INDEX_NAME: ClassVar[str] = "pg_idx_emb_e5_1024__{surface}_{aspect}__hnsw"

    def __init__(self, cfg: WorkerConfig, package_dir: Path) -> None:
        self._cfg = cfg
        self._dir = package_dir
        embedding = LocalEmbedding(
            kind="local",
            model=cfg.model,
            cache_dir=cfg.cache_dir,
            dim=cfg.dim,
            batch_size=cfg.batch,
            progress_every=cfg.batch,
        )
        self._embedder = EmbedderFactory.build(embedding)
        self._chunker = Chunker(cfg.cache_dir, cfg.chunk_tokens, cfg.chunk_overlap)

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
            chunks: list[list[str]] = []
            for row in rows:
                chunks.append(self._chunker.split(row.content))
            flat: list[str] = []
            for parts in chunks:
                flat.extend(parts)
            vectors = await self._embed(flat)
            offset = 0
            for row, parts in zip(rows, chunks, strict=True):
                await self._write(
                    conn, sql_files, row, parts, vectors[offset : offset + len(parts)]
                )
                offset += len(parts)
            await self._unlock(conn, sql_files)
            rounds += 1
            written += len(rows)
            logger.info(
                "round %d: %d aspects written as %d chunks",
                rounds,
                len(rows),
                len(flat),
            )
        return rounds, written

    async def _queue(
        self, conn: psycopg.AsyncConnection[Any], sql_files: PackageSql
    ) -> list[QueueRow]:
        cur = await conn.execute(
            sql_files.load(SqlFile.QUEUE), {"batch": self._cfg.batch}
        )
        rows: list[QueueRow] = []
        for node_id, surface, aspect, content, content_hash in await cur.fetchall():
            rows.append(
                QueueRow(
                    node_id=node_id,
                    surface=surface,
                    aspect=aspect,
                    content=content,
                    content_hash=content_hash,
                )
            )
        return rows

    async def _embed(self, contents: Sequence[str]) -> Sequence[Sequence[float]]:
        try:
            vectors = await self._embedder.embed_documents(contents)
        except EmbeddingError as exc:
            raise VectorWorkerError(
                f"embedding {len(contents)} chunks with {self._cfg.model}: {exc}"
            ) from exc
        if len(vectors) != len(contents):
            raise VectorWorkerError(
                f"embedding {len(contents)} chunks: expected {len(contents)} vectors, "
                "got {len(vectors)}"
            )
        return vectors

    async def _write(
        self,
        conn: psycopg.AsyncConnection[Any],
        sql_files: PackageSql,
        row: QueueRow,
        parts: Sequence[str],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        rendered: list[str] = []
        for vector in vectors:
            rendered.append("[" + ",".join(f"{value:.6g}" for value in vector) + "]")
        params = {
            "node_id": row.node_id,
            "surface": row.surface,
            "aspect": row.aspect,
            "content_hash": row.content_hash,
            "chunk_count": len(parts),
            "chunk_nos": list(range(len(parts))),
            "contents": list(parts),
            "embs": rendered,
        }
        await conn.execute(sql_files.load(SqlFile.WRITE), params)

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
    """Команда и путь к конфигу; настройки берутся из секции [ix.idx_vector]."""

    SECTION: ClassVar[str] = "ix.idx_vector"

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> tuple[Command, Path]:
        parser = argparse.ArgumentParser(
            prog="boba-pg-idx-vector",
            description=(
                "Векторный индексатор pg_idx_emb_e5_1024: схема пакета и цикл чанков."
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
                "профиль подключения к базе ix, берутся из секции [ix.idx_vector]."
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
    except (ConfigError, SchemaUpgradeError, VectorWorkerError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
