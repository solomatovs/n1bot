"""Boba extension: read-only ChromaDB knowledge-base tools.

Регистрируется в боба через entry-point boba.tools (см.
pyproject.toml). ToolPluginLoader при старте процесса вызывает
register_tools — функция возвращает один StaticToolSource
с двумя tools (kb_list_collections, kb_search) под общим
ToolSourceId("ext.chromadb").

Зависит на chromadb runtime + boba core API. chromadb-клиент
живёт как singleton процесса, инстанцируется лениво при первом вызове
любого tool.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.domain.core.tools import StaticToolSource, ToolSource, ToolSourceId
from boba.ext.chromadb.config import ChromadbSection
from boba.ext.chromadb.kb import get_knowledge_base
from boba.ext.chromadb.kb_list_collections import KbListCollectionsTool
from boba.ext.chromadb.kb_search import KbSearchTool
from boba.infra.tool_plugin_loader import ExtensionContext

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: возвращает 2 read-only tools одним
    источником.

    Конфиг расширения достаётся из бандла через
    ctx.config.section(ChromadbSection). Сама ChromadbSection
    регистрируется в ConfigFactory через парный entry-point
    boba.config_sections (см. pyproject.toml); если её там нет —
    ctx.config.section(ChromadbSection) бросит
    ConfigSectionMissingError.
    """
    cfg = ctx.config.section(ChromadbSection)
    kb = get_knowledge_base(cfg)
    yield StaticToolSource(
        ToolSourceId("ext.chromadb"),
        priority=0,
        tools=[
            KbListCollectionsTool(kb),
            KbSearchTool(kb, cfg),
        ],
    )
