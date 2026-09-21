"""Эмбеддинг аспектов: чанки по токенизатору модели, векторы провайдером проекта,
запись набора чанков одного аспекта в таблицу эмбеддингов.

Библиотечная часть pg-idx-vector. Воркер pg зовёт её в своём цикле для
pg_idx_emb_e5_1024, индексаторы других происхождений — в своём, со своей
таблицей: имя приходит в конструктор и подставляется в run/20_write.sql
вместо `{table}`.

Ошибки:
AspectEmbeddingError — токенизатор модели не найден, окно чанка не больше
    перекрытия, провайдер недоступен или отдал не столько векторов, сколько
    просили.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field
from tokenizers import Tokenizer

from boba.llm.embedding import EmbedderFactory, EmbeddingError, LocalEmbedding
from boba.pg_ix_core.schema_name import SchemaName

__all__ = [
    "AspectEmbedding",
    "AspectEmbeddingError",
    "AspectText",
    "Chunker",
    "EmbeddingParams",
]

logger = logging.getLogger("aspect-embedding")


class AspectEmbeddingError(Exception):
    """Эмбеддинг аспектов не удался: модель, чанкер или провайдер."""


class EmbeddingParams(BaseModel):
    """Параметры модели и нарезки; секция конфига любого индексатора наследует их."""

    model_config = ConfigDict(extra="ignore")

    model: str = "intfloat/multilingual-e5-large"
    cache_dir: str
    dim: int = Field(gt=0, default=1024)
    batch: int = Field(gt=0, default=64)
    chunk_tokens: int = Field(gt=0, default=400)
    chunk_overlap: int = Field(ge=0, default=50)


class AspectText(BaseModel):
    """Текст одного аспекта node на вход эмбеддингу; content_hash — md5 всего текста."""

    node_id: int
    surface: str
    aspect: str
    content: str
    content_hash: str


class WriteFile(StrEnum):
    """Файл записи набора чанков; лежит в run/ пакета pg-idx-vector."""

    WRITE = "20_write.sql"


class Chunker:
    """Режет текст аспекта на окна по токенам модели с перекрытием. Токенизатор
    берётся из того же кэша fastembed, что и модель, поэтому границы совпадают
    с тем, что видит модель. Текст короче окна остаётся одним чанком."""

    TOKENIZER_GLOB: ClassVar[str] = "models--*/snapshots/*/tokenizer.json"

    def __init__(self, cache_dir: str, chunk_tokens: int, overlap: int) -> None:
        if overlap >= chunk_tokens:
            raise AspectEmbeddingError(
                f"chunking: overlap {overlap} must be smaller than chunk size "
                f"{chunk_tokens}"
            )

        found = sorted(Path(cache_dir).glob(self.TOKENIZER_GLOB))
        if not found:
            raise AspectEmbeddingError(
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


class AspectEmbedding:
    """Чанки, векторы и запись набора чанков аспекта в таблицу эмбеддингов.

    Один экземпляр на процесс: держит модель. write() принимает пачку аспектов,
    режет каждый, кодирует все чанки пачки одним вызовом провайдера и на каждый
    аспект выполняет 20_write.sql; замена набора чанков атомарна в statement'е.
    """

    def __init__(self, params: EmbeddingParams, db_schema: str, table: str) -> None:
        self._params = params
        self._db_schema = db_schema
        self._table = table
        self._dir = Path(__file__).resolve().parent / "run"
        embedding = LocalEmbedding(
            kind="local",
            model=params.model,
            cache_dir=params.cache_dir,
            dim=params.dim,
            batch_size=params.batch,
            progress_every=params.batch,
        )
        self._embedder = EmbedderFactory.build(embedding)
        self._chunker = Chunker(
            params.cache_dir, params.chunk_tokens, params.chunk_overlap
        )

    @property
    def model(self) -> str:
        return self._params.model

    async def write(
        self, conn: psycopg.AsyncConnection[Any], rows: Sequence[AspectText]
    ) -> int:
        """Записать чанки всех аспектов пачки; возвращает число чанков."""
        chunks: list[list[str]] = []
        for row in rows:
            chunks.append(self._chunker.split(row.content))

        flat: list[str] = []
        for parts in chunks:
            flat.extend(parts)

        vectors = await self._embed(flat)

        offset = 0
        for row, parts in zip(rows, chunks, strict=True):
            await self._write_one(
                conn, row, parts, vectors[offset : offset + len(parts)]
            )
            offset += len(parts)

        return len(flat)

    async def _embed(self, contents: Sequence[str]) -> Sequence[Sequence[float]]:
        if not contents:
            return []

        try:
            vectors = await self._embedder.embed_documents(contents)
        except EmbeddingError as exc:
            raise AspectEmbeddingError(
                f"embedding {len(contents)} chunks with {self._params.model}: {exc}"
            ) from exc

        if len(vectors) != len(contents):
            raise AspectEmbeddingError(
                f"embedding {len(contents)} chunks with {self._params.model}: "
                f"expected {len(contents)} vectors, got {len(vectors)}"
            )

        return vectors

    async def _write_one(
        self,
        conn: psycopg.AsyncConnection[Any],
        row: AspectText,
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
        text = (self._dir / WriteFile.WRITE).read_text(encoding="utf-8")
        query = SchemaName.render(
            text, self._db_schema, table=sql.Identifier(self._table)
        )
        await conn.execute(query, params)
