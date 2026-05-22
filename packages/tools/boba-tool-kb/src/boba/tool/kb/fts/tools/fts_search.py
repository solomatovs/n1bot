"""Tool `fts_search` + `FtsSearchConfig`: FTS-поиск по таблице оператора.

Не имеет отношения к KB — это read-only websearch по чужой таблице
оператора, описанной через `IndexSpec`. Таблица фиксирована конфигом;
LLM передаёт только `query` + опц. `top_k`.

DSN может отличаться от KB-store: оператор может закрепить read-only
роль с ограниченным `GRANT SELECT` на одну whitelist-таблицу.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.core.postgres_connection import PostgresConnection
from boba.tool.kb.core.postgres_pool import open_kb_pool
from boba.tool.kb.fts.db import FtsQueryError, PgFtsKnowledgeBase
from boba.tool.kb.fts.models import IndexSpec
from boba.tools import FromConfig, tool

__all__ = ["FtsSearchConfig", "fts_search"]


class FtsSearchConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `fts_search`.

    Config-секция: `[tool.kb.fts_search]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.fts_search",
    )

    connection: PostgresConnection
    index: IndexSpec
    snippet_options: str = Field(
        default="MaxFragments=2,MaxWords=35,MinWords=15",
        description=(
            "Опции `ts_headline`: MaxFragments,MaxWords,MinWords,"
            "StartSel,StopSel,..."
        ),
    )
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра top_k для fts_search.",
    )


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
    pool = open_kb_pool(cfg.connection)
    kb = PgFtsKnowledgeBase(
        pool=pool,
        index=cfg.index,
        snippet_options=cfg.snippet_options,
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
