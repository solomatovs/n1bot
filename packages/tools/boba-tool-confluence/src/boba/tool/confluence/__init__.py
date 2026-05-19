"""boba-tool-confluence — плагин: 5 онлайн-tools поверх Confluence REST.

Tools:
- `confluence_search`                  — CQL-поиск по тексту.
- `confluence_page_outline`            — структура заголовков страницы.
- `confluence_page_section`            — текст одной секции по page_id+anchor.
- `confluence_page_download`           — скачать страницы как HTML.
- `confluence_page_download_markdown`  — скачать страницы как Markdown.

Все шарят `ConfluencePluginConfig` (`[tool.confluence]`,
`BOBA_TOOL__CONFLUENCE__*`); `enable=true` подключает их пакетом,
allowlist `tools` сужает. `confluence_page_download*` тулзы требуют
`ProjectWorkspaceShell` через FromDI.
"""

from __future__ import annotations

from boba.tool.confluence.config import ConfluencePluginConfig
from boba.tool.confluence.page_download import confluence_page_download
from boba.tool.confluence.page_download_markdown import (
    confluence_page_download_markdown,
)
from boba.tool.confluence.page_outline import confluence_page_outline
from boba.tool.confluence.page_section import confluence_page_section
from boba.tool.confluence.search import confluence_search

__all__ = [
    "ConfluencePluginConfig",
    "confluence_page_download",
    "confluence_page_download_markdown",
    "confluence_page_outline",
    "confluence_page_section",
    "confluence_search",
]
