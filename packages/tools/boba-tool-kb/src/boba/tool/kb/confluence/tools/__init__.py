"""Confluence-tools: tool-функции, регистрируемые в фреймворке.

Online (read-only API):
- `confluence_search`        — CQL-поиск.
- `confluence_spaces_list`   — список доступных spaces (markdown-таблица).

Download (в workspace):
- `confluence_page_download`  — скачать список page_ids (HTML/Markdown).
- `confluence_space_download` — скачать все страницы space-а (HTML/Markdown).

Ingest (в KB):
- `confluence_page_ingest`        — индексация явного списка page_ids.
- `confluence_space_ingest`       — индексация одного/нескольких spaces.

Операторские CLI-runner'ы вынесены в `confluence/cli/`:
- `ingest_all_spaces`             — discover + ingest всех доступных spaces.
"""
