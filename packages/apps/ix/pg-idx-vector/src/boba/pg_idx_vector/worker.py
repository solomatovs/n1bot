"""Воркер векторного индексатора: очередь из ix, резка на чанки, эмбеддинг провайдером
проекта, запись набора чанков аспекта.

Один цикл: 10_queue.sql пачкой, текст каждого аспекта режется токенизатором модели
на окна с перекрытием, embed_documents провайдера boba.llm.embedding по чанкам пачки,
20_write.sql на каждый аспект (весь набор его чанков одним statement'ом), 90_unlock.sql,
и так до пустой очереди; в конце 30_prune.sql. SQL-файлы читаются как есть, параметры
передаются словарём по именам.

Ошибки:
VectorWorkerError — база или провайдер недоступны, ответ не того вида, что ожидался.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field
from tokenizers import Tokenizer

from boba.config import ConfigError, bind_section
from boba.llm.embedding import EmbedderFactory, EmbeddingError, LocalEmbedding
from boba.pg_ix_core.schema_name import SchemaName, StorageSchema
from boba.pg_ix_core.upgrade import (
    SchemaUpgrade,
    SchemaUpgradeError,
    UpgradeConfig,
)

logger = logging.getLogger("pg-idx-vector")


class VectorWorkerError(Exception):
    """Ошибка цикла воркера: база, провайдер или контракт файлов пакета."""


class SqlFile(StrEnum):
    QUEUE = "10_queue.sql"
    WRITE = "20_write.sql"
    PRUNE = "30_prune.sql"
    UNLOCK = "90_unlock.sql"


class WorkerConfig(StorageSchema):
    dsn: str
    model: str = "intfloat/multilingual-e5-large"
    cache_dir: str
    dim: int = Field(gt=0, default=1024)
    batch: int = Field(gt=0, default=64)
    chunk_tokens: int = Field(gt=0, default=400)
    chunk_overlap: int = Field(ge=0, default=50)
    lock_timeout: str = "2s"
    statement_timeout: str = "60s"


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

    def load(self, name: SqlFile, conn: psycopg.Connection) -> bytes:
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


class VectorWorker:
    """Цикл индексатора: одна сессия к ix, один эмбеддер проекта."""

    def __init__(self, cfg: WorkerConfig, sql_files: PackageSql) -> None:
        self._cfg = cfg
        self._sql = sql_files
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
            with psycopg.connect(
                self._cfg.dsn, autocommit=True, application_name="pg-idx-vector"
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
                try:
                    rounds, written = await self._upsert_rounds(conn)
                finally:
                    self._unlock(conn)
                pruned = self._prune(conn)
                return CycleReport(rounds=rounds, written=written, pruned=pruned)
        except psycopg.Error as exc:
            raise VectorWorkerError(f"ix database {self._cfg.dsn}: {exc}") from exc

    async def _upsert_rounds(self, conn: psycopg.Connection) -> tuple[int, int]:
        rounds = 0
        written = 0
        while True:
            rows = self._queue(conn)
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
                self._write(conn, row, parts, vectors[offset : offset + len(parts)])
                offset += len(parts)
            self._unlock(conn)
            rounds += 1
            written += len(rows)
            logger.info(
                "round %d: %d aspects written as %d chunks",
                rounds,
                len(rows),
                len(flat),
            )
        return rounds, written

    def _queue(self, conn: psycopg.Connection) -> list[QueueRow]:
        cur = conn.execute(
            self._sql.load(SqlFile.QUEUE, conn), {"batch": self._cfg.batch}
        )
        rows: list[QueueRow] = []
        for node_id, surface, aspect, content, content_hash in cur.fetchall():
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

    def _write(
        self,
        conn: psycopg.Connection,
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
        conn.execute(self._sql.load(SqlFile.WRITE, conn), params)

    def _unlock(self, conn: psycopg.Connection) -> None:
        conn.execute(self._sql.load(SqlFile.UNLOCK, conn))

    def _prune(self, conn: psycopg.Connection) -> int:
        cur = conn.execute(self._sql.load(SqlFile.PRUNE, conn))
        record = cur.fetchone()
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
                "строку подключения к базе ix, берутся из секции [ix.idx_vector]."
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
        worker = VectorWorker(cfg, PackageSql(package_dir / "run", cfg.db_schema))
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
