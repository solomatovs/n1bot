"""Tool `vector_search`: pure vector (cosine) semantic search по KB-коллекциям.

Параллелен `kb_search` (hybrid RRF) и `fts_search` (pure FTS) — четвёртый
поисковый tool, на этот раз только vector-канал. Полезен, когда FTS-канал
шумит/мешает (короткие запросы, эмбеддинг лучше ловит синонимы).

Список целевых коллекций — `[tool.kb.search].collections` (как у `kb_search`).
LLM передаёт только `query` + `top_k`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field

from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.core.errors import KnowledgeBaseError
from boba.tool.kb.core.kb import PostgresKnowledgeBase
from boba.tool.kb.core.search_config import SearchConfig
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
    search_cfg: Annotated[SearchConfig, FromConfig()],
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
    """Pure vector (cosine) semantic search по KB-коллекциям.

    Ищет внутри коллекций, наполненных через `files_ingest` /
    `confluence_space_ingest` / `confluence_page_ingest`. Список целевых
    коллекций — pre-настроенный оператором (`[tool.kb.search].collections`);
    LLM их не выбирает. SQL-уровень: `WHERE collection = ANY(...)`.

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
            collections=list(search_cfg.collections),
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
