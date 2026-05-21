"""Confluence-сабпакет внутри boba-tool-kb: онлайн-tools поверх Confluence REST.

Tools:
- `confluence_search`                  — CQL-поиск по тексту.
- `confluence_page_outline`            — структура заголовков страницы.
- `confluence_page_section`            — текст одной секции по page_id+anchor.
- `confluence_page_download`           — скачать страницы как HTML.
- `confluence_page_download_markdown`  — скачать страницы как Markdown.

Плагин-уровневое включение управляется секцией `[tool.kb]` родителя
(вместе с kb_search/kb_list_collections/kb_ingest). Confluence-tools шарят
`ConfluenceConnectionConfig` (`[tool.kb.confluence]`,
`BOBA_TOOL__KB__CONFLUENCE__*`) — connection (base_url/auth/timeout).
`confluence_page_download*` дополнительно требуют `ProjectWorkspaceShell`
через FromDI.

Те же `ConfluenceJsonDecoder`/`ConfluenceReader`/`ConfluenceConnection` +
`ConfluencePages|Cql|SpaceRequestSource` переиспользуются tool'ом
`kb_ingest_confluence` (см. соседний `kb_ingest_confluence.py`) для
индексации Confluence-страниц в pgvector-коллекцию через тот же
`StreamingIndexer` pipeline, что и FS-ingest.
"""

from __future__ import annotations

from boba.tool.kb.confluence.config import ConfluencePluginConfig
from boba.tool.kb.confluence.page_download import confluence_page_download
from boba.tool.kb.confluence.page_download_markdown import (
    confluence_page_download_markdown,
)
from boba.tool.kb.confluence.page_outline import confluence_page_outline
from boba.tool.kb.confluence.page_section import confluence_page_section
from boba.tool.kb.confluence.search import confluence_search

__all__ = [
    "ConfluencePluginConfig",
    "confluence_page_download",
    "confluence_page_download_markdown",
    "confluence_page_outline",
    "confluence_page_section",
    "confluence_search",
]
