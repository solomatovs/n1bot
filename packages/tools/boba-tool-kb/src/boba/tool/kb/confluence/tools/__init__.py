"""Confluence-tools: tool-функции, регистрируемые в фреймворке.

Каждая подпапка отражает `<verb>` из конфиг-иерархии
`[tool.kb.confluence.<verb>.<target>]`:

- `ingest/`   — `confluence_ingest_{space,page,cql}` (HTTP → KB chunks).
- `search/`   — `confluence_search_cql` (online CQL).
- `download/` — `confluence_download_{page,space}` (HTTP → workspace).
- `list/`     — `confluence_list_spaces` (discovery markdown-таблица).

Операторские CLI-runner'ы — в `boba.tool.kb.cli.confluence.*`.
"""
