"""Вызов kb-payload'а: эмбеддинг и SQL идут внутри песочницы."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit2.sandbox import SandboxCaller, SandboxEntryConfig

__all__ = ["KbCaller", "KbSearchAnswer", "KbSearchRequest"]


class KbSearchRequest(BaseModel):
    """Поиск по чанкам: SQL-шаблон приходит с хоста, данные — из БД."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    op: str = Field(min_length=1)
    connection: dict[str, Any]
    sql_template: str = Field(min_length=1)
    schema_name: str = Field(min_length=1, alias="schema")
    chunks_table: str = Field(min_length=1)
    collections: tuple[str, ...]
    query: str = Field(min_length=1)
    top_k: int = Field(ge=1)
    snippet_chars: int = Field(ge=1)
    embedding: dict[str, str] = Field(
        description="Модель эмбеддера и каталог весов внутри песочницы.",
    )


class KbSearchAnswer(BaseModel):
    """Строки выдачи; в SearchHit их превращает приложение."""

    model_config = ConfigDict(extra="forbid")

    rows: tuple[dict[str, Any], ...]


class KbCaller:
    """Один вызов payload'а на поиск: модель грузится внутри заново."""

    VECTOR_OP = "kb_vector_search"
    FTS_OP = "kb_fts_search"

    def __init__(
        self,
        tool: str,
        sandbox: SandboxEntryConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._entry = sandbox.entry
        self._caller = SandboxCaller(tool, sandbox.effective(), path_vars)

    def search(  # noqa: PLR0913 — параметры поиска независимы
        self,
        *,
        op: str,
        connection: Mapping[str, Any],
        sql_template: str,
        schema: str,
        chunks_table: str,
        collections: Sequence[str],
        query: str,
        top_k: int,
        snippet_chars: int,
        embedding: Mapping[str, str],
    ) -> KbSearchAnswer:
        request = KbSearchRequest(
            op=op,
            connection=dict(connection),
            sql_template=sql_template,
            schema=schema,
            chunks_table=chunks_table,
            collections=tuple(collections),
            query=query,
            top_k=top_k,
            snippet_chars=snippet_chars,
            embedding=dict(embedding),
        )
        return self._caller.call_json(self._entry, request, KbSearchAnswer)
