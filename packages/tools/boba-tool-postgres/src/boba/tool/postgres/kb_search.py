"""Tool: гибридный (vector + FTS, RRF) semantic search по одной KB-коллекции."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field

from boba.tool.postgres.config import PostgresPluginConfig
from boba.tool.postgres.errors import KnowledgeBaseError
from boba.tool.postgres.kb import PostgresKnowledgeBase
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
    cfg: Annotated[PostgresPluginConfig, FromConfig()],
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
    """Hybrid semantic search по KB-коллекции (vector + FTS + RRF).

    Возвращает JSON-массив hits {id, distance, link, metadata, snippet},
    упорядоченный по релевантности (меньшее distance = ближе/релевантнее).
    Перед вызовом узнай доступные коллекции через kb_list_collections.
    """
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    try:
        hits = kb.search(
            collection=cfg.ingest_collection,
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
