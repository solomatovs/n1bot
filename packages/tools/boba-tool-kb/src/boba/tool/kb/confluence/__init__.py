"""Confluence-сабпакет внутри boba-tool-kb.

Tools (каждый — self-contained tool-конфиг в своей TOML-секции):

Ingest (Confluence → kb_chunks) — три отдельных tool'а, одна общая секция
`[tool.kb.confluence.ingest]`:
- `confluence_ingest_spaces`  — индексировать все страницы spaces.
- `confluence_ingest_pages`   — индексировать явный список page_id.
- `confluence_ingest_cql`     — индексировать страницы по CQL-запросу.

Search (online):
- `confluence_search_cql`     — CQL-search по реальному Confluence.
                                Секция `[tool.kb.confluence.search.cql]`.

List/Discovery:
- `confluence_list_spaces`    — список доступных spaces (markdown).
                                Секция `[tool.kb.confluence.list.spaces]`.

Download (Confluence → workspace):
- `confluence_download`       — unified: space_keys / page_ids
                                (LLM выбирает режим). Секция
                                `[tool.kb.confluence.download]`.

`ConfluenceConnection` (basemodel из `connection.py`) встраивается как
nested-поле в каждый tool-конфиг — common-connection-секции нет, каждый
tool полностью изолирован.
"""

from __future__ import annotations

from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.download import (
    ConfluenceDownloadConfig,
    confluence_download,
)
from boba.tool.kb.confluence.fetch import (
    ConfluenceFetchPageConfig,
    confluence_fetch_page,
)
from boba.tool.kb.confluence.ingest import (
    ConfluenceIngestConfig,
    confluence_ingest_cql,
    confluence_ingest_pages,
    confluence_ingest_spaces,
)
from boba.tool.kb.confluence.list_spaces import (
    ConfluenceListSpacesConfig,
    confluence_list_spaces,
)
from boba.tool.kb.confluence.search_cql import (
    ConfluenceSearchCqlConfig,
    confluence_search_cql,
)

__all__ = [
    "ConfluenceConnection",
    "ConfluenceDownloadConfig",
    "ConfluenceFetchPageConfig",
    "ConfluenceIngestConfig",
    "ConfluenceListSpacesConfig",
    "ConfluenceSearchCqlConfig",
    "confluence_download",
    "confluence_fetch_page",
    "confluence_ingest_cql",
    "confluence_ingest_pages",
    "confluence_ingest_spaces",
    "confluence_list_spaces",
    "confluence_search_cql",
]
