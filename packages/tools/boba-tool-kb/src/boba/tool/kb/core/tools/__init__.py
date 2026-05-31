"""Core-tools: tool-функции, не привязанные к одному внешнему домену.

- `search/vector` — `kb_search_vector` (pure vector, cosine) по
                    `[tool.kb.search.vector].collections`.
- `search/fts`    — `kb_search_fts` (pure FTS, `ts_rank_cd`) по
                    `[tool.kb.search.fts].collections`.
"""
