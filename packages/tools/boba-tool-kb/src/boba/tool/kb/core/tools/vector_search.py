"""Tool `vector_search` + `VectorSearchConfig`: pure vector (cosine) поверх KB.

Параллелен `kb_search` (hybrid RRF) и `fts_search` (pure FTS) — четвёртый
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

__all__ = ["VectorSearchConfig", "vector_search"]


class VectorSearchConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `vector_search`.

    Config-секция: `[tool.kb.vector_search]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.vector_search",
        defaults_from=("postgres", "kb.storage", "embedding"),
    )

    knowledge_base: PostgresKnowledgeBaseConfig
    collections: StringList = Field(
        default_factory=lambda: ["kb_files", "kb_confluence"],
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
def vector_search(
    cfg: Annotated[VectorSearchConfig, FromConfig()],
    query: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Поисковый запрос на естественном языке — будет преобразован "
                "в embedding, далее cosine-distance (`<=>`) против вектора "
                "каждого чанка. Без FTS-канала."
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
    """Pure vector (cosine) semantic search по KB-коллекциям.

    Возвращает JSON-массив hits `{id, distance, link, metadata, snippet}`,
    упорядоченный по релевантности (меньше distance = ближе). `distance` —
    cosine-distance pgvector'а в [0..2].
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
