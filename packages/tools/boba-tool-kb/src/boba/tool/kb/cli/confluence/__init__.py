"""Confluence CLI runners: download / ingest операции над реальным Confluence
или над уже-скачанной папкой.

- `ingest/folder`  — индексация уже-скачанной папки confluence-download'ов.
- `ingest/spaces`  — bulk-ingest всех discovered space'ов (HTTP-загрузка).
- `download/page`  — скачать явный список page_ids.
- `download/space` — скачать весь space.
- `download/spaces` — bulk-download всех discovered space'ов.
"""
