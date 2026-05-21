"""Subpackage `external_fts`: read-only FTS-поиск по чужим таблицам оператора.

Tools:
- `fts_search(index, query, top_k)`  — websearch_to_tsquery по whitelist-индексу.
- `fts_list_indexes()`               — список доступных индексов с описаниями.

Whitelist индексов задаётся через `ExternalFtsConfig.indexes`
(`[tool.kb.external_fts]`): каждый `IndexSpec` описывает таблицу, колонки
и язык. LLM видит только то, что объявлено в конфиге; авто-discovery по
`information_schema` намеренно не делается.

Pool шарится с основной KB через DI-инжекцию `PostgresPool` (см.
`providers.py`): если `external_fts.dsn` пуст или совпадает с
`[tool.kb].dsn`, используется тот же singleton-Pool, что и у kb_search.
"""

from __future__ import annotations

from boba.tool.kb.external_fts.config import ExternalFtsConfig
from boba.tool.kb.external_fts.fts_list_indexes import fts_list_indexes
from boba.tool.kb.external_fts.fts_search import fts_search
from boba.tool.kb.external_fts.models import FtsHit, IndexInfo, IndexSpec
from boba.tool.kb.external_fts.providers import provide_external_fts_kb

__all__ = [
    "ExternalFtsConfig",
    "FtsHit",
    "IndexInfo",
    "IndexSpec",
    "fts_list_indexes",
    "fts_search",
    "provide_external_fts_kb",
]
