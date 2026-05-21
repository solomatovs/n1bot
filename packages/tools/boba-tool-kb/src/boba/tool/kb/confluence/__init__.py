"""Confluence-сабпакет внутри boba-tool-kb.

Tools:
- `confluence_search`         — CQL-поиск по реальному Confluence (online).
- `confluence_page_download`  — скачать список page_ids (HTML или Markdown).
- `confluence_space_download` — скачать все страницы space-а (HTML или Markdown).
- `confluence_space_ingest`   — индексация всех страниц space в KB.
- `confluence_page_ingest`    — индексация явного списка page_ids в KB.

Общий connection — `[tool.kb.confluence]` (`ConfluenceConnectionConfig`).
"""

from __future__ import annotations

from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.page_download import confluence_page_download
from boba.tool.kb.confluence.page_ingest import confluence_page_ingest
from boba.tool.kb.confluence.search import confluence_search
from boba.tool.kb.confluence.space_download import confluence_space_download
from boba.tool.kb.confluence.space_ingest import confluence_space_ingest

__all__ = [
    "ConfluenceConnectionConfig",
    "confluence_page_download",
    "confluence_page_ingest",
    "confluence_search",
    "confluence_space_download",
    "confluence_space_ingest",
]
