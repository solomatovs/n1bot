"""Tool `vector_search`: pure vector (cosine) semantic search по KB-коллекции.

Параллелен `kb_search` (hybrid RRF) и `fts_search` (pure FTS) — четвёртый
поисковый tool, на этот раз только vector-канал. Полезен, когда FTS-канал
шумит/мешает (короткие запросы, эмбеддинг лучше ловит синонимы).

Target collection — pinned `[tool.kb].collection` (как у `kb_search`).
LLM передаёт только `query` + `top_k`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field

from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.core.errors import KnowledgeBaseError
from boba.tool.kb.core.kb import PostgresKnowledgeBase
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["vector_search"]


@tool
def vector_search(
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
    kb: Annotated[PostgresKnowledgeBase, FromDI(Scope.APP)],
    cfg: Annotated[KbConfig, FromConfig()],
    top_k: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Сколько hits вернуть. По умолчанию 5; жёсткий потолок — "
                "в конфиге (`[tool.kb].max_top_k`)."
            ),
        ),
    ] = 5,
) -> list[dict[str, Any]]:
    """Pure vector (cosine) semantic search по нашей KB-коллекции.

    Ищет внутри коллекции, наполненной через `files_ingest` /
    `confluence_space_ingest` / `confluence_page_ingest`. Целевая
    коллекция — pre-настроенная оператором (`[tool.kb].collection`);
    LLM её не выбирает.

    Когда выбирать `vector_search` vs остальные:
    - **vector_search** — наша KB, чистый vector (cosine). `distance` —
      cosine-distance pgvector'а [0..2], меньше = ближе. Лучше для
      запросов с синонимами/перефразированием.
    - **kb_search** — наша KB, **hybrid** (vector + FTS, RRF). Лучше для
      запросов с точными терминами + смысловым подтекстом.
    - **fts_search** — pre-настроенная таблица оператора (`[tool.kb.fts]`);
      чистый FTS без embedding'ов.
    - **confluence_search** — online CQL по реальному Confluence (не KB).

    Возвращает JSON-массив hits `{id, distance, link, metadata, snippet}`,
    упорядоченный по релевантности (меньше distance = ближе).
    """
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    try:
        hits = kb.vector_search(
            collection=cfg.collection,
            query=query,
            top_k=top_k,
        )
    except KnowledgeBaseError as e:
        raise RuntimeError(str(e)) from e

    return [
        {
            "id": h.id,
            "distance": h.distance,
            "link": _build_link(h.metadata),
            "metadata": dict(h.metadata),
            "snippet": h.snippet,
        }
        for h in hits
    ]


def _build_link(metadata: Mapping[str, str]) -> str:
    """source_url[#anchor] — готовый deep-link, чтобы агент не склеивал сам."""
    url = str(metadata.get("source_url") or "")
    if not url:
        return ""
    anchor = str(metadata.get("anchor") or "")
    return f"{url}#{anchor}" if anchor else url
