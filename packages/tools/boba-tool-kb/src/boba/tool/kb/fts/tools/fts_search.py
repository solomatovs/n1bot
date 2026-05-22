"""Tool `fts_search`: FTS-поиск по таблице оператора.

Не имеет отношения к KB — это read-only websearch по чужой таблице
оператора, описанной через `IndexSpec`. Таблица фиксирована конфигом;
LLM передаёт только `query` + опц. `top_k`.

Конфиг (`FtsSearchConfig`) и сервис (`PgFtsKnowledgeBase`) живут в одном
модуле `fts/db.py`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.kb.fts.db import FtsQueryError, FtsSearchConfig, PgFtsKnowledgeBase
from boba.tools import FromConfig, tool

__all__ = ["fts_search"]


@tool
def fts_search(
    cfg: Annotated[FtsSearchConfig, FromConfig()],
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
    top_k: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Сколько hits вернуть. По умолчанию 5; жёсткий потолок — "
                "в `cfg.max_top_k`."
            ),
        ),
    ] = 5,
) -> list[dict[str, Any]]:
    """Чистый FTS-поиск (ts_rank_cd + ts_headline) по pre-настроенной таблице.

    Это НЕ поиск по нашей KB (для неё — `kb_search`, hybrid vector+FTS+RRF
    по `kb_chunks`). Здесь — read-only websearch по таблице оператора,
    описанной в `cfg.index`. Таблица фиксирована, LLM передаёт только
    `query` + `top_k`.

    Возвращает JSON-массив hits `{id, score, metadata, snippet}`,
    упорядоченный по релевантности (score = `ts_rank_cd`, больше = ближе).
    """
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    kb = PgFtsKnowledgeBase(cfg=cfg)
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
