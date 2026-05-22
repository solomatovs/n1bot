"""Subpackage `fts`: read-only FTS-поиск по одной таблице оператора.

Tools:
- `fts_search(query, top_k)`  — websearch_to_tsquery по таблице из конфига.

Таблица задаётся через `FtsConfig.index` (`[tool.kb.fts]`): один `IndexSpec`
описывает таблицу/колонки/язык. LLM таблицу не выбирает — она pinned
оператором.

Pool шарится с основной KB через DI-инжекцию `PostgresPool` (см.
`providers.py`): если `fts.dsn` пуст, используется тот же singleton-Pool,
что и у kb_search.
"""

from __future__ import annotations

from boba.tool.kb.fts.config import FtsConfig
from boba.tool.kb.fts.tools.fts_search import fts_search
from boba.tool.kb.fts.models import FtsHit, IndexSpec
from boba.tool.kb.fts.providers import provide_fts_kb

__all__ = [
    "FtsConfig",
    "FtsHit",
    "IndexSpec",
    "fts_search",
    "provide_fts_kb",
]
