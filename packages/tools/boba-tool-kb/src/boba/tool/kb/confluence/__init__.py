"""Confluence-сабпакет внутри boba-tool-kb.

Tools (каждый — self-contained tool-конфиг в своей TOML-секции):

Ingest (Confluence → kb_chunks):
- `confluence_ingest_space`   — все страницы перечисленных space'ов.
                                Секция `[tool.kb.confluence.ingest.space]`.
- `confluence_ingest_page`    — явный список page_ids.
                                Секция `[tool.kb.confluence.ingest.page]`.
- `confluence_ingest_cql`     — страницы, отобранные CQL-запросом.
                                Секция `[tool.kb.confluence.ingest.cql]`.

Search (online):
- `confluence_search_cql`     — CQL-search по реальному Confluence.
                                Секция `[tool.kb.confluence.search.cql]`.

List/Discovery:
- `confluence_list_spaces`    — список доступных spaces (markdown).
                                Секция `[tool.kb.confluence.list.spaces]`.

Download (Confluence → workspace):
- `confluence_download_page`  — скачать список page_ids (HTML или Markdown).
                                Секция `[tool.kb.confluence.download.page]`.
- `confluence_download_space` — скачать все страницы space (HTML или Markdown).
                                Секция `[tool.kb.confluence.download.space]`.

`ConfluenceConnection` (basemodel из `connection.py`) встраивается как
nested-поле в каждый tool-конфиг — common-connection-секции нет, каждый
tool полностью изолирован.
"""

from __future__ import annotations

from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.tools.download.page import (
    ConfluenceDownloadPageConfig,
    confluence_download_page,
)
from boba.tool.kb.confluence.tools.download.space import (
    ConfluenceDownloadSpaceConfig,
    confluence_download_space,
)
from boba.tool.kb.confluence.tools.ingest.cql import (
    ConfluenceIngestCqlConfig,
    confluence_ingest_cql,
)
from boba.tool.kb.confluence.tools.ingest.page import (
    ConfluenceIngestPageConfig,
    confluence_ingest_page,
)
from boba.tool.kb.confluence.tools.ingest.space import (
    ConfluenceIngestSpaceConfig,
    confluence_ingest_space,
)
from boba.tool.kb.confluence.tools.list.spaces import (
    ConfluenceListSpacesConfig,
    confluence_list_spaces,
)
from boba.tool.kb.confluence.tools.search.cql import (
    ConfluenceSearchCqlConfig,
    confluence_search_cql,
)

__all__ = [
    "ConfluenceConnection",
    "ConfluenceDownloadPageConfig",
    "ConfluenceDownloadSpaceConfig",
    "ConfluenceIngestCqlConfig",
    "ConfluenceIngestPageConfig",
    "ConfluenceIngestSpaceConfig",
    "ConfluenceListSpacesConfig",
    "ConfluenceSearchCqlConfig",
    "confluence_download_page",
    "confluence_download_space",
    "confluence_ingest_cql",
    "confluence_ingest_page",
    "confluence_ingest_space",
    "confluence_list_spaces",
    "confluence_search_cql",
]
