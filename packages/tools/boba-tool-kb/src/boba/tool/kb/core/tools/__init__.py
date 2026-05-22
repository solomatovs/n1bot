"""Core-tools: tool-функции, не привязанные к одному внешнему домену.

- `kb_search`       — hybrid (vector + FTS, RRF) по `[tool.kb].collection`.
- `vector_search`   — pure vector (cosine) по `[tool.kb].collection`.
- `files_ingest`    — индексация FS-папки (`[tool.kb].files_folder`) в KB.
"""
