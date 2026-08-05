"""Вызов kb-payload'а: эмбеддинг и SQL идут внутри песочницы."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from boba.db.postgres import PostgresConfig
from boba.toolkit.launcher import ChunkSink, LauncherFactory

__all__ = ["KbCaller", "KbSearchRequest", "KbSearchTrailer"]


class KbSearchRequest(BaseModel):
    """Поиск по чанкам: SQL-шаблон приходит с хоста, данные — из БД."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1)
    connection: PostgresConfig = Field(
        description=(
            "Профиль подключения целиком: payload сам получает по нему TGT из keytab."
        ),
    )
    sql_template: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    chunks_table: str = Field(min_length=1)
    collections: tuple[str, ...]
    query: str = Field(min_length=1)
    top_k: int = Field(ge=1)
    snippet_chars: int = Field(ge=1)
    embedding: dict[str, str] = Field(
        description="Модель эмбеддера и каталог весов внутри песочницы.",
    )

    @field_serializer("connection", when_used="json")
    def _dump_connection(self, value: PostgresConfig) -> dict[str, Any]:
        """stdin песочницы — доверенный канал: только здесь пароль едет раскрытым."""
        return value.model_dump(
            mode="json",
            context={PostgresConfig.REVEAL_SECRETS: True},
        )


class KbSearchTrailer(BaseModel):
    """Итог поиска: строки выдачи ушли кадрами-записями."""

    model_config = ConfigDict(extra="forbid")


class KbCaller:
    """Один вызов payload'а на поиск: модель грузится внутри заново."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.kb.payload")

    VECTOR_OP = "kb_vector_search"
    FTS_OP = "kb_fts_search"

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._caller = launchers(tool)

    def search(  # noqa: PLR0913
        self,
        *,
        op: str,
        connection: PostgresConfig,
        sql_template: str,
        schema_name: str,
        chunks_table: str,
        collections: Sequence[str],
        query: str,
        top_k: int,
        snippet_chars: int,
        embedding: Mapping[str, str],
        sink: ChunkSink,
    ) -> KbSearchTrailer:
        request = KbSearchRequest(
            op=op,
            connection=connection,
            sql_template=sql_template,
            schema_name=schema_name,
            chunks_table=chunks_table,
            collections=tuple(collections),
            query=query,
            top_k=top_k,
            snippet_chars=snippet_chars,
            embedding=dict(embedding),
        )
        return self._caller.call_stream(self.ENTRY, request, sink, KbSearchTrailer)
