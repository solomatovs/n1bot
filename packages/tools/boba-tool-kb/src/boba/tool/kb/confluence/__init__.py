"""Confluence-сабпакет внутри boba-tool-kb.

Tools (каждый — self-contained tool-конфиг в своей TOML-секции):

Ingest (Confluence → kb_chunks):
- `confluence_ingest`         — unified: space_keys / page_ids / cql
                                (LLM выбирает режим). Секция
                                `[tool.kb.confluence.ingest]`.

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
from boba.tool.kb.confluence.tools.download import (
    ConfluenceDownloadConfig,
    confluence_download,
)
from boba.tool.kb.confluence.tools.ingest import (
    ConfluenceIngestConfig,
    confluence_ingest,
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
    "ConfluenceDownloadConfig",
    "ConfluenceIngestConfig",
    "ConfluenceListSpacesConfig",
    "ConfluenceSearchCqlConfig",
    "confluence_download",
    "confluence_ingest",
    "confluence_list_spaces",
    "confluence_search_cql",
]
