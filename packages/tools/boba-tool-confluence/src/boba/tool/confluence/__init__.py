"""boba-tool-confluence — v2 плагин: 5 онлайн-tools поверх Confluence REST.

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
from boba.tool.confluence.page_download import ConfluencePageDownloadTool
from boba.tool.confluence.page_download_markdown import (
    ConfluencePageDownloadMarkdownTool,
)
from boba.tool.confluence.page_outline import ConfluencePageOutlineTool
from boba.tool.confluence.page_section import ConfluencePageSectionTool
from boba.tool.confluence.search import ConfluenceSearchTool

__all__ = [
    "ConfluencePageDownloadMarkdownTool",
    "ConfluencePageDownloadTool",
    "ConfluencePageOutlineTool",
    "ConfluencePageSectionTool",
    "ConfluencePluginConfig",
    "ConfluenceSearchTool",
]
