"""Воркер векторного индексатора: очередь из ix, эмбеддинг провайдером проекта, запись.

Один цикл: 10_queue.sql пачкой, embed_documents провайдера boba.llm.embedding,
20_write.sql на каждую строку, 90_unlock.sql, и так до пустой очереди; в конце 30_prune.sql.
SQL-файлы пакета читаются как есть, плейсхолдеры $N переводятся в параметры psycopg.

Ошибки:
VectorWorkerError — база или провайдер недоступны, ответ не того вида, что ожидался.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from enum import StrEnum
from pathlib import Path
from typing import Sequence

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field

from boba.llm.embedding import EmbedderFactory, EmbeddingError, LocalEmbedding

logger = logging.getLogger("pg-indexer-vector")


class VectorWorkerError(Exception):
    """Ошибка цикла воркера: база, провайдер или контракт файлов пакета."""


class SqlFile(StrEnum):
    QUEUE = "10_queue.sql"
    WRITE = "20_write.sql"
    PRUNE = "30_prune.sql"
    UNLOCK = "90_unlock.sql"


class WorkerConfig(BaseModel):
    dsn: str
    model: str = "intfloat/multilingual-e5-large"
    cache_dir: str
    dim: int = Field(gt=0, default=1024)
    batch: int = Field(gt=0, default=64)
    rounds: int = Field(ge=0, default=0)
    prune: bool = True
    lock_timeout: str = "2s"
    statement_timeout: str = "60s"


class QueueRow(BaseModel):
    node_id: int
    surface: str
    aspect: str
    content: str


class CycleReport(BaseModel):
    rounds: int
    written: int
    pruned: int


class PackageSql:
    """Файлы пакета рядом с воркером; $N заменяется на %s, параметры раскладываются по порядку
    вхождений. Текст отдаётся байтами: psycopg принимает запрос как LiteralString, bytes или sql.SQL."""

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


class VectorWorker:
    """Цикл индексатора: одна сессия к ix, один эмбеддер проекта."""

    def __init__(self, cfg: WorkerConfig, sql: PackageSql) -> None:
        self._cfg = cfg
        self._sql = sql
        embedding = LocalEmbedding(kind="local", model=cfg.model, cache_dir=cfg.cache_dir, dim=cfg.dim, batch_size=cfg.batch, progress_every=cfg.batch)
        self._embedder = EmbedderFactory.build(embedding)

    async def run(self) -> CycleReport:
        try:
            with psycopg.connect(self._cfg.dsn, autocommit=True, application_name="pg-indexer-vector") as conn:
                conn.execute(sql.SQL("set lock_timeout = {}").format(sql.Literal(self._cfg.lock_timeout)))
                conn.execute(sql.SQL("set statement_timeout = {}").format(sql.Literal(self._cfg.statement_timeout)))
                try:
                    rounds, written = await self._upsert_rounds(conn)
                finally:
                    self._unlock(conn)
                pruned = 0
                if self._cfg.prune:
                    pruned = self._prune(conn)
                return CycleReport(rounds=rounds, written=written, pruned=pruned)
        except psycopg.Error as exc:
            raise VectorWorkerError(f"ix database {self._cfg.dsn}: {exc}") from exc

    async def _upsert_rounds(self, conn: psycopg.Connection) -> tuple[int, int]:
        rounds = 0
        written = 0
        while self._cfg.rounds == 0 or rounds < self._cfg.rounds:
            rows = self._queue(conn)
            if not rows:
                break
            vectors = await self._embed(rows)
            for row, vector in zip(rows, vectors):
                self._write(conn, row, vector)
            self._unlock(conn)
            rounds += 1
            written += len(rows)
            logger.info("round %d: %d rows written", rounds, len(rows))
        return rounds, written

    def _queue(self, conn: psycopg.Connection) -> list[QueueRow]:
        text, order = self._sql.load(SqlFile.QUEUE)
        cur = conn.execute(text, PackageSql.bind(order, [self._cfg.batch]))
        rows: list[QueueRow] = []
        for node_id, surface, aspect, content in cur.fetchall():
            rows.append(QueueRow(node_id=node_id, surface=surface, aspect=aspect, content=content))
        return rows

    async def _embed(self, rows: Sequence[QueueRow]) -> Sequence[Sequence[float]]:
        contents: list[str] = []
        for row in rows:
            contents.append(row.content)
        try:
            vectors = await self._embedder.embed_documents(contents)
        except EmbeddingError as exc:
            raise VectorWorkerError(f"embedding {len(contents)} texts with {self._cfg.model}: {exc}") from exc
        if len(vectors) != len(rows):
            raise VectorWorkerError(f"embedding {len(rows)} texts: expected {len(rows)} vectors, got {len(vectors)}")
        return vectors

    def _write(self, conn: psycopg.Connection, row: QueueRow, vector: Sequence[float]) -> None:
        text, order = self._sql.load(SqlFile.WRITE)
        rendered = "[" + ",".join(f"{value:.6g}" for value in vector) + "]"
        conn.execute(text, PackageSql.bind(order, [row.node_id, row.surface, row.aspect, row.content, rendered]))

    def _unlock(self, conn: psycopg.Connection) -> None:
        text, _ = self._sql.load(SqlFile.UNLOCK)
        conn.execute(text)

    def _prune(self, conn: psycopg.Connection) -> int:
        text, _ = self._sql.load(SqlFile.PRUNE)
        cur = conn.execute(text)
        record = cur.fetchone()
        if record is None:
            raise VectorWorkerError("prune: expected one summary row, got none")
        return int(record[1])


class Cli:
    """Аргументы командной строки в WorkerConfig."""

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> WorkerConfig:
        parser = argparse.ArgumentParser(description="pg-indexer-vector worker")
        parser.add_argument("--dsn", required=True)
        parser.add_argument("--cache-dir", required=True)
        parser.add_argument("--model", default="intfloat/multilingual-e5-large")
        parser.add_argument("--dim", type=int, default=1024)
        parser.add_argument("--batch", type=int, default=64)
        parser.add_argument("--rounds", type=int, default=0)
        parser.add_argument("--no-prune", action="store_true")
        args = parser.parse_args(argv)
        return WorkerConfig(dsn=args.dsn, cache_dir=args.cache_dir, model=args.model, dim=args.dim, batch=args.batch, rounds=args.rounds, prune=not args.no_prune)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = Cli.parse()
    worker = VectorWorker(cfg, PackageSql(Path(__file__).resolve().parent))
    report = asyncio.run(worker.run())
    logger.info("done: rounds=%d written=%d pruned=%d", report.rounds, report.written, report.pruned)


if __name__ == "__main__":
    main()
