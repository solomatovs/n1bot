"""boba.tool.postgres_fts — read-only PG FTS tools.

Tools:
- `fts_search(index, query, top_k)` — websearch_to_tsquery по whitelist-индексу.
- `fts_list_indexes()` — список доступных индексов с описаниями.

Whitelist индексов задаётся через `PostgresFtsPluginConfig.indexes`: каждый
`IndexSpec` описывает таблицу, колонки и язык. LLM видит только то, что
объявлено в конфиге; авто-discovery по `information_schema` намеренно
не делается.
"""

from __future__ import annotations

from boba.tool.postgres_fts.fts_list_indexes import (
    FtsListIndexesArgs,
    FtsListIndexesTool,
    FtsListIndexesToolConfig,
)
from boba.tool.postgres_fts.fts_search import (
    FtsSearchArgs,
    FtsSearchTool,
    FtsSearchToolConfig,
)
from boba.tool.postgres_fts.models import FtsHit, IndexInfo, IndexSpec
from boba.tool.postgres_fts.plugin import (
    PostgresFtsPlugin,
    PostgresFtsPluginConfig,
)

__all__ = [
    "FtsHit",
    "FtsListIndexesArgs",
    "FtsListIndexesTool",
    "FtsListIndexesToolConfig",
    "FtsSearchArgs",
    "FtsSearchTool",
    "FtsSearchToolConfig",
    "IndexInfo",
    "IndexSpec",
    "PostgresFtsPlugin",
    "PostgresFtsPluginConfig",
]
