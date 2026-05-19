"""Tool: semantic search по одной KB-коллекции."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field

from boba.tool.chromadb.config import ChromadbPluginConfig
from boba.tool.chromadb.enable import chromadb_enable_if
from boba.tool.chromadb.errors import (
    CollectionNotFoundError,
    KnowledgeBaseError,
)
from boba.tool.chromadb.kb import ChromaKnowledgeBase
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["KbSearchTool"]


@tool(enable_if=chromadb_enable_if("kb_search"))
class KbSearchTool:
    """Semantic search по KB-коллекции ChromaDB.

    Возвращает JSON-массив hits {id, distance, link, metadata, snippet},
    упорядоченный по релевантности (меньшее distance = ближе). Перед
    вызовом узнай доступные коллекции через kb_list_collections.
    """

    def __call__(
        self,
        collection: Annotated[
            str,
            Field(min_length=1, description="Имя коллекции из kb_list_collections."),
        ],
        query: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Поисковый запрос на естественном языке — будет преобразован "
                    "в embedding и сопоставлен с документами коллекции."
                ),
            ),
        ],
        kb: Annotated[ChromaKnowledgeBase, FromDI(Scope.APP)],
        cfg: Annotated[ChromadbPluginConfig, FromConfig()],
        top_k: Annotated[
            int,
            Field(
                ge=1,
                description=(
                    "Сколько hits вернуть. По умолчанию 5; жёсткий "
                    "потолок задан в конфиге плагина (`max_top_k`)."
                ),
            ),
        ] = 5,
    ) -> list[dict[str, Any]]:
        if top_k > cfg.max_top_k:
            raise RuntimeError(
                f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
            )
        try:
            hits = kb.search(collection=collection, query=query, top_k=top_k)
        except CollectionNotFoundError as e:
            raise RuntimeError(
                f"collection {e.name!r} not found; "
                f"call kb_list_collections to see available ones",
            ) from e
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
