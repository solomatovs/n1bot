"""Confluence-сабпакет внутри boba-tool-kb.

Tools (каждый — self-contained tool-конфиг в своей TOML-секции):

Ingest (Confluence → kb_chunks):
- `confluence_space_ingest`   — все страницы перечисленных space'ов.
                                Секция `[tool.kb.confluence.ingest.space]`.
- `confluence_page_ingest`    — явный список page_ids.
                                Секция `[tool.kb.confluence.ingest.page]`.
- `confluence_cql_ingest`     — страницы, отобранные CQL-запросом.
                                Секция `[tool.kb.confluence.ingest.cql]`.

Search (online):
- `confluence_cql_search`     — CQL-search по реальному Confluence.
                                Секция `[tool.kb.confluence.search.cql]`.
- `confluence_spaces_list`    — список доступных spaces (markdown).
                                Секция `[tool.kb.confluence.spaces_list]`.

Download (Confluence → workspace):
- `confluence_page_download`  — скачать список page_ids (HTML или Markdown).
                                Секция `[tool.kb.confluence.download.page]`.
- `confluence_space_download` — скачать все страницы space (HTML или Markdown).
                                Секция `[tool.kb.confluence.download.space]`.

`ConfluenceConnection` (basemodel из `connection.py`) встраивается как
nested-поле в каждый tool-конфиг — common-connection-секции нет, каждый
tool полностью изолирован.
"""

from __future__ import annotations

from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.tools.cql_ingest import (
    ConfluenceCqlIngestConfig,
    confluence_cql_ingest,
)
from boba.tool.kb.confluence.tools.cql_search import (
    ConfluenceCqlSearchConfig,
    confluence_cql_search,
)
from boba.tool.kb.confluence.tools.page_download import (
    ConfluencePageDownloadConfig,
    confluence_page_download,
)
from boba.tool.kb.confluence.tools.page_ingest import (
    ConfluencePageIngestConfig,
    confluence_page_ingest,
)
from boba.tool.kb.confluence.tools.space_download import (
    ConfluenceSpaceDownloadConfig,
    confluence_space_download,
)
from boba.tool.kb.confluence.tools.space_ingest import (
    ConfluenceSpaceIngestConfig,
    confluence_space_ingest,
)
from boba.tool.kb.confluence.tools.spaces_list import (
    ConfluenceSpacesListConfig,
    confluence_spaces_list,
)

__all__ = [
    "ConfluenceConnection",
    "ConfluenceCqlIngestConfig",
    "ConfluenceCqlSearchConfig",
    "ConfluencePageDownloadConfig",
    "ConfluencePageIngestConfig",
    "ConfluenceSpaceDownloadConfig",
    "ConfluenceSpaceIngestConfig",
    "ConfluenceSpacesListConfig",
    "confluence_cql_ingest",
    "confluence_cql_search",
    "confluence_page_download",
    "confluence_page_ingest",
    "confluence_space_download",
    "confluence_space_ingest",
    "confluence_spaces_list",
]
