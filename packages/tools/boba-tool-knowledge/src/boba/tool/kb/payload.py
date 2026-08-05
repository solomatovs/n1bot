"""Операции базы знаний в песочнице: ONNX-инференс над недоверенным текстом
не идёт в процессе приложения, ценой перезагрузки модели на каждый вызов."""

from __future__ import annotations

import sys
from typing import Any, ClassVar

from psycopg import sql
from psycopg.rows import dict_row

from boba.db.postgres import PayloadPostgres
from boba.toolkit.launcher import RowStream
from boba.toolkit.payload import ChunkEmitter, PayloadEntry


class KbOps:
    """Векторный и полнотекстовый поиск по чанкам."""

    OPS: ClassVar[tuple[str, ...]] = ("kb_vector_search", "kb_fts_search")

    @classmethod
    async def dispatch(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        op = request["op"]
        if op == "kb_vector_search":
            return await cls.vector_search(request, emit)
        if op == "kb_fts_search":
            return await cls.fts_search(request, emit)
        msg = f"unknown kb op: {op!r}"
        raise ValueError(msg)

    @staticmethod
    def embed(request: dict[str, Any]) -> tuple[list[float], int]:
        """Вектор запроса и его размерность — их ждёт SQL-шаблон."""
        from fastembed import (  # noqa: PLC0415 # pyright: ignore[reportMissingImports]
            TextEmbedding,
        )

        model = request["embedding"]
        encoder = TextEmbedding(
            model_name=model["model"], cache_dir=model["cache_dir"]
        )
        vector = next(iter(encoder.embed([request["query"]])))
        values: list[float] = []
        for item in vector:
            values.append(float(item))
        return values, len(values)

    @classmethod
    async def vector_search(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        vector, dim = cls.embed(request)
        statement = sql.SQL(request["sql_template"]).format(
            dim=sql.Literal(dim),
            chunks_table=cls.table(request),
        )
        params = {
            "collections": list(request["collections"]),
            "embedding": vector,
            "snippet_chars": request["snippet_chars"],
            "top_k": request["top_k"],
        }
        await cls.select(request, statement, params, emit)
        return {}

    @classmethod
    async def fts_search(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        statement = sql.SQL(request["sql_template"]).format(
            chunks_table=cls.table(request),
            schema=sql.Identifier(request["schema_name"]),
        )
        params = {
            "collections": list(request["collections"]),
            "query": request["query"],
            "snippet_chars": request["snippet_chars"],
            "top_k": request["top_k"],
        }
        await cls.select(request, statement, params, emit)
        return {}

    @staticmethod
    def table(request: dict[str, Any]) -> sql.Identifier:
        return sql.Identifier(request["schema_name"], request["chunks_table"])

    @classmethod
    async def select(
        cls,
        request: dict[str, Any],
        statement: sql.Composed,
        params: dict[str, Any],
        emit: ChunkEmitter,
    ) -> None:
        """Каждая строка выдачи уходит кадром-записью, не задерживаясь в памяти."""
        conn = await PayloadPostgres.connect(request)
        async with conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(statement, params)
            async for row in cur:
                emit(RowStream.encode(PayloadPostgres.jsonable(row)))


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(KbOps.dispatch))
