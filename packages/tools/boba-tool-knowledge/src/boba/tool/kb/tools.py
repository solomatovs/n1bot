"""Инструмент поиска по KB; коллекция зашита, канал (vector/fts) — аргумент."""

from typing import Annotated

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.tool.kb.caller import KbCaller
from boba.tool.kb.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.protocol import KbSearchMethod
from boba.tool.kb.search import (
    CollectionSearch,
    ConfluenceCollection,
    KbSearch,
)
from boba.toolkit.launcher import LauncherFactory
from boba.toolkit.result import ToolResult, pack_result

__all__ = ["KbTools", "build_kb_tools"]


class KbTools:
    """Собирает langchain-инструменты поиска поверх KbSearch."""

    def __init__(
        self,
        cfg: PostgresKnowledgeBaseConfig,
        launchers: LauncherFactory,
    ) -> None:
        self._cfg = cfg
        self._caller = KbCaller("kb", launchers)

    def build(self) -> list[BaseTool]:
        return [self._search(ConfluenceCollection)]

    def _search(self, collection: type[CollectionSearch]) -> BaseTool:
        cfg = self._cfg
        caller = self._caller

        @tool(
            description=KbSearch.TOOL_DESC,
            response_format="content_and_artifact",
        )
        def kb_search(
            query: Annotated[
                str,
                Field(min_length=1, description=KbSearch.QUERY_DESC),
            ],
            method: Annotated[
                KbSearchMethod,
                Field(description=KbSearch.METHOD_DESC),
            ] = KbSearchMethod.VECTOR,
            top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
            snippet_chars: Annotated[
                int,
                Field(ge=1, description=KbSearch.SNIPPET_DESC),
            ] = KbSearch.SNIPPET_DEFAULT,
        ) -> tuple[str, ToolResult]:
            return pack_result(
                KbSearch.run(
                    cfg, caller, collection, query, method, top_k, snippet_chars
                )
            )

        return kb_search


def build_kb_tools(
    cfg: PostgresKnowledgeBaseConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return KbTools(cfg, launchers).build()
