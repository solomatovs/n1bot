"""Tool `fts_search`: FTS-поиск по одной whitelist-таблице оператора.

Таблица фиксирована конфигом (`[tool.kb.fts].index`); LLM её не выбирает.
LLM передаёт только query + top_k.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.kb.fts.config import FtsConfig
from boba.tool.kb.fts.db import FtsQueryError, PgFtsKnowledgeBase
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["fts_search"]


@tool
def fts_search(
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
    cfg: Annotated[FtsConfig, FromConfig()],
    top_k: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Сколько hits вернуть. По умолчанию 5; жёсткий потолок — "
                "в конфиге (`[tool.kb.fts].max_top_k`)."
            ),
        ),
    ] = 5,
) -> list[dict[str, Any]]:
    """Чистый FTS-поиск (ts_rank_cd + ts_headline) по pre-настроенной таблице.

    Это НЕ поиск по нашей KB (для неё — `kb_search`, hybrid vector+FTS+RRF
    по `kb_chunks`). Здесь — read-only websearch по таблице оператора,
    описанной в `[tool.kb.fts].index`. Таблица фиксирована, LLM передаёт
    только `query` + `top_k`.

    Возвращает JSON-массив hits `{id, score, metadata, snippet}`,
    упорядоченный по релевантности (score = `ts_rank_cd`, больше = ближе).
    """
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    try:
        hits = kb.search(query=query, top_k=top_k)
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
