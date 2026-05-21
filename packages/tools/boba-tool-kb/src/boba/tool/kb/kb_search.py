"""Tool: гибридный (vector + FTS, RRF) semantic search по одной KB-коллекции."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field

from boba.tool.kb.config import KbConfig
from boba.tool.kb.errors import KnowledgeBaseError
from boba.tool.kb.kb import PostgresKnowledgeBase
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["kb_search"]


@tool
def kb_search(
    query: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Поисковый запрос на естественном языке — будет преобразован "
                "в embedding (для vector-канала) и в plainto_tsquery (для "
                "FTS-канала). Гибридный результат склеивается через RRF."
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
                "Сколько hits вернуть. По умолчанию 5; жёсткий потолок "
                "задан в конфиге плагина (`max_top_k`)."
            ),
        ),
    ] = 5,
) -> list[dict[str, Any]]:
    """Hybrid semantic search (vector + FTS + RRF) по нашей KB-коллекции.

    Ищет внутри коллекции, наполненной через `files_ingest` (FS),
    `confluence_space_ingest` или `confluence_page_ingest` — это
    таблица `kb_chunks`, разбитая на коллекции колонкой `collection`.
    Целевая коллекция — pre-настроенная оператором (`[tool.kb].collection`,
    default `knowledge_base`); LLM её не выбирает.

    Когда выбирать `kb_search` vs `fts_search`:
    - **kb_search** — наша KB; нужны embedding-релевантность и
      контекстные чанки; шкала: меньше distance = ближе. Возвращает
      `{id, distance, link, metadata, snippet}`.
    - **fts_search** — pre-настроенная таблица оператора (`[tool.kb.fts].index`);
      чистый FTS без embedding'ов.

    Возвращает JSON-массив hits, упорядоченный по релевантности
    (меньшее distance = ближе/релевантнее).
    """
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    try:
        hits = kb.search(
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
