"""Subpackage `fts`: read-only FTS-поиск по одной таблице оператора.

Tools:
- `fts_search(query, top_k)` — websearch_to_tsquery по таблице из конфига.
  Секция `[tool.kb.fts_search]`. Таблица фиксируется `FtsSearchConfig.index`
  (`IndexSpec`): один whitelist-индекс на tool. LLM таблицу не выбирает.

Каждый tool self-contained: connection + index + snippet_options + max_top_k
прямо в его TOML-секции (через рекурсивный flatten в `BobaFlatSettings`).
"""

from __future__ import annotations

from boba.tool.kb.fts.db import FtsSearchConfig, PgFtsKnowledgeBase
from boba.tool.kb.fts.models import FtsHit, IndexSpec
from boba.tool.kb.fts.tools.fts_search import fts_search

__all__ = [
    "FtsHit",
    "FtsSearchConfig",
    "IndexSpec",
    "PgFtsKnowledgeBase",
    "fts_search",
]
