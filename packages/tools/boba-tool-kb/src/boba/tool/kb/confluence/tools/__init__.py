"""Confluence-tools: tool-функции, регистрируемые в фреймворке.

Структура по `<verb>` из конфиг-иерархии `[tool.kb.confluence.<verb>]`:

- `ingest.py`   — `confluence_ingest_spaces` / `confluence_ingest_pages` /
                  `confluence_ingest_cql` (HTTP → KB chunks; общая секция
                  `[tool.kb.confluence.ingest]`).
- `download.py` — `confluence_download` (unified: space_keys / page_ids,
                  HTTP → workspace).
- `search/`     — `confluence_search_cql` (online CQL).
- `list/`       — `confluence_list_spaces` (discovery markdown-таблица).

Операторские CLI-runner'ы — в `boba.tool.kb.cli.confluence.*`.
"""
