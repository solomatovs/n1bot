"""Tool: полнотекстовый поиск в одном PG-FTS индексе."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.postgres_fts.config import PostgresFtsPluginConfig
from boba.tool.postgres_fts.db import (
    FtsQueryError,
    IndexNotFoundError,
    PgFtsKnowledgeBase,
)
from boba.tool.postgres_fts.enable import pg_fts_enable_if
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["FtsSearchTool"]


@tool(enable_if=pg_fts_enable_if("fts_search"))
class FtsSearchTool:
    """Полнотекстовый поиск в PostgreSQL по whitelist'ed-индексу.

    Возвращает JSON-массив hits {id, score, metadata, snippet}, упорядоченный
    по релевантности (score = ts_rank_cd, больше = ближе). Перед вызовом
    узнай доступные индексы через fts_list_indexes.
    """

    def __call__(
        self,
        index: Annotated[
            str, Field(min_length=1, description="Имя индекса из fts_list_indexes."),
        ],
        query: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Поисковый запрос. Поддерживается websearch-синтаксис: "
                    'кавычки для фраз ("exact phrase"), OR для альтернатив, '
                    "минус-слово для исключения."
                ),
            ),
        ],
        kb: Annotated[PgFtsKnowledgeBase, FromDI(Scope.APP)],
        cfg: Annotated[PostgresFtsPluginConfig, FromConfig()],
        top_k: Annotated[
            int,
            Field(
                ge=1,
                description=(
                    "Сколько hits вернуть. По умолчанию 5; жёсткий потолок "
                    "задан в конфиге плагина (`max_top_k`)."
                ),
            ),
        ] = 5,
    ) -> list[dict[str, Any]]:
        if top_k > cfg.max_top_k:
            raise RuntimeError(
                f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
            )
        try:
            hits = kb.search(index=index, query=query, top_k=top_k)
        except IndexNotFoundError as e:
            raise RuntimeError(
                f"fts index {e.name!r} not found; "
                f"call fts_list_indexes to see available ones",
            ) from e
        except FtsQueryError as e:
            raise RuntimeError(str(e)) from e

        return [
            {
                "id": h.id,
                "score": h.score,
                "metadata": dict(h.metadata),
                "snippet": h.snippet,
            }
            for h in hits
        ]
