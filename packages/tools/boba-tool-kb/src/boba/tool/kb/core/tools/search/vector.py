"""Tool `kb_search_vector` + `KbSearchVectorConfig`: pure vector (cosine) поверх KB.

Параллелен `kb_search_hybrid` (RRF) и `fts_search` (pure FTS) — четвёртый
поисковый tool, на этот раз только vector-канал. Полезен, когда FTS-канал
шумит/мешает (короткие запросы, эмбеддинг лучше ловит синонимы).

LLM передаёт только `query` + опц. `top_k`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList
from boba.tool.kb.core.errors import KnowledgeBaseError
from boba.tool.kb.core.kb import (
    PostgresKnowledgeBase,
    PostgresKnowledgeBaseConfig,
)
from boba.tools import FromConfig, tool

__all__ = ["KbSearchVectorConfig", "kb_search_vector"]


class KbSearchVectorConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `kb_search_vector`.

    Config-секция: `[tool.kb.search.vector]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.search.vector",
        defaults_from=("postgres", "kb.storage", "embedding"),
    )

    knowledge_base: PostgresKnowledgeBaseConfig
    collections: StringList = Field(
        default_factory=lambda: ["kb_kbdoc", "kb_confluence"],
        description=(
            "Список коллекций для pure vector-search. SQL: "
            "`WHERE collection = ANY(%(collections)s)`."
        ),
    )
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра `top_k`.",
    )


@tool
def kb_search_vector(
    cfg: Annotated[KbSearchVectorConfig, FromConfig()],
    query: Annotated[
        str,
        Field(
            min_length=1,
            description="Поисковый запрос на естественном языке"
        ),
    ],
    top_k: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Сколько hits вернуть. По умолчанию 5"
            ),
        ),
    ] = 5,
) -> list[dict[str, Any]]:
    """semantic search по KB-коллекциям

    Возвращает JSON-массив hits `{id, distance, link, metadata, snippet}`,
    упорядоченный по релевантности (меньше distance = ближе)
    """
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    kb = PostgresKnowledgeBase(cfg=cfg.knowledge_base)
    try:
        return [
            {
                "id": h.id,
                "distance": h.distance,
                "link": _build_link(h.metadata),
                "metadata": dict(h.metadata),
                "snippet": h.snippet,
            }
            for h in kb.vector_search(
                collections=list(cfg.collections),
                query=query,
                top_k=top_k,
            )
        ]
    except KnowledgeBaseError as e:
        raise RuntimeError(str(e)) from e


def _build_link(metadata: Mapping[str, str]) -> str:
    """source_url[#anchor] — готовый deep-link, чтобы агент не склеивал сам."""
    url = str(metadata.get("source_url") or "")
    if not url:
        return ""
    anchor = str(metadata.get("anchor") or "")
    return f"{url}#{anchor}" if anchor else url
