"""Поиск по проиндексированной базе знаний: 4 инструмента.

Канал (vector/fts) и коллекция зашиты в сам инструмент — параметра method нет.
Проекция результатов живёт в search.py, здесь только обёртки langchain.
"""

from typing import Annotated

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.chainlit2.agent.tools.kb.kb import PostgresKnowledgeBaseConfig
from boba.chainlit2.agent.tools.kb.search import (
    CollectionSearch,
    ConfluenceCollection,
    KbDocCollection,
    KbSearch,
    SearchMethod,
)
from boba.chainlit2.rendering.render import pack_result
from boba.chainlit2.rendering.tool_result import ToolResult

__all__ = ["KbTools", "build_kb_tools"]


class KbTools:
    """Собирает langchain-инструменты поиска поверх KbSearch."""

    def __init__(self, cfg: PostgresKnowledgeBaseConfig) -> None:
        self._cfg = cfg

    def build(self) -> list[BaseTool]:
        return [
            self._search(
                "kb_vector_search",
                ConfluenceCollection,
                "vector",
                "Семантический (vector) поиск по коллекции Confluence-страниц.",
            ),
            self._search(
                "kb_fts_search",
                ConfluenceCollection,
                "fts",
                "Полнотекстовый (fts) поиск по коллекции Confluence-страниц.",
            ),
            self._search(
                "kb_doc_vector_search",
                KbDocCollection,
                "vector",
                "Семантический (vector) поиск по коллекции документов kb_doc.",
            ),
            self._search(
                "kb_doc_fts_search",
                KbDocCollection,
                "fts",
                "Полнотекстовый (fts) поиск по коллекции документов kb_doc.",
            ),
        ]

    def _search(
        self,
        name: str,
        collection: type[CollectionSearch],
        method: SearchMethod,
        summary: str,
    ) -> BaseTool:
        cfg = self._cfg
        query_desc = (
            KbSearch.QUERY_DESC_VECTOR
            if method == "vector"
            else KbSearch.QUERY_DESC_FTS
        )

        description = (
            f"{summary}\n\nВозвращает таблицу hits с колонками id/distance/"
            "link/snippet и метаданными, по релевантности."
        )

        @tool(name, description=description, response_format="content_and_artifact")
        def search(
            query: Annotated[str, Field(min_length=1, description=query_desc)],
            top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
            snippet_chars: Annotated[
                int,
                Field(ge=1, description=KbSearch.SNIPPET_DESC),
            ] = KbSearch.SNIPPET_DEFAULT,
        ) -> tuple[str, ToolResult]:
            return pack_result(
                KbSearch.run(cfg, collection, query, method, top_k, snippet_chars)
            )

        return search


def build_kb_tools(cfg: PostgresKnowledgeBaseConfig) -> list[BaseTool]:
    return KbTools(cfg).build()
