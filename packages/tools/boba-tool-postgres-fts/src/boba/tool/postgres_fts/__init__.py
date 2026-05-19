"""boba.tool.postgres_fts — read-only PG FTS tools (v2 plugin).

Tools:
- `fts_search(index, query, top_k)` — websearch_to_tsquery по whitelist-индексу.
- `fts_list_indexes()` — список доступных индексов с описаниями.

Whitelist индексов задаётся через `PostgresFtsPluginConfig.indexes`: каждый
`IndexSpec` описывает таблицу, колонки и язык. LLM видит только то, что
объявлено в конфиге; авто-discovery по `information_schema` намеренно
не делается.

Plugin-module для `AgentBuilder.use_plugin(boba.tool.postgres_fts)` и
discovery через `[project.entry-points."boba.plugins"]`. На module
scope экспортируются `@tool` классы и `@provides` factories — `AgentBuilder`
обходит и регистрирует помеченные объекты.
"""

from __future__ import annotations

from boba.tool.postgres_fts.config import PostgresFtsPluginConfig
from boba.tool.postgres_fts.fts_list_indexes import FtsListIndexesTool
from boba.tool.postgres_fts.fts_search import FtsSearchTool
from boba.tool.postgres_fts.models import FtsHit, IndexInfo, IndexSpec
from boba.tool.postgres_fts.providers import provide_kb, provide_pool

__all__ = [
    "FtsHit",
    "FtsListIndexesTool",
    "FtsSearchTool",
    "IndexInfo",
    "IndexSpec",
    "PostgresFtsPluginConfig",
    "provide_kb",
    "provide_pool",
]
