"""Core-tools: tool-функции, не привязанные к одному внешнему домену.

- `kb_search`       — hybrid (vector + FTS, RRF) по `[tool.kb.search].collections`.
- `vector_search`   — pure vector (cosine) по `[tool.kb.search].collections`.
- `files_ingest`    — индексация FS-папки (`[tool.kb.files].folder`) в KB.
"""
