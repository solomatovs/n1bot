"""Confluence CLI runners: download / ingest операции над реальным Confluence
или над уже-скачанной папкой.

- `ingest/folder`  — индексация уже-скачанной папки confluence-download'ов.
- `ingest/http`    — unified HTTP-ingest: bulk-discovery / `only`-список
                     spaces / явный `page_ids`-список (выбор по фильтрам).
- `download/http`  — unified HTTP-download: те же три режима скачивания.
"""
