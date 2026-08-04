"""Вызов kb-payload'а: эмбеддинг и SQL идут внутри песочницы."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from boba.db.postgres import PostgresConfig
from boba.toolkit.sandbox import SandboxCaller, SandboxToolConfig

__all__ = ["KbCaller", "KbSearchAnswer", "KbSearchRequest"]


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


class KbSearchAnswer(BaseModel):
    """Строки выдачи; в SearchHit их превращает приложение."""

    model_config = ConfigDict(extra="forbid")

    rows: tuple[dict[str, Any], ...]


class KbCaller:
    """Один вызов payload'а на поиск: модель грузится внутри заново."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.kb.payload")

    VECTOR_OP = "kb_vector_search"
    FTS_OP = "kb_fts_search"

    def __init__(
        self,
        tool: str,
        sandbox: SandboxToolConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._caller = SandboxCaller(tool, sandbox.effective(), path_vars)

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
    ) -> KbSearchAnswer:
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
        return self._caller.call_json(self.ENTRY, request, KbSearchAnswer)
