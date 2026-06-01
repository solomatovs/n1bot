"""boba-tool-doc — плагин: чтение и поиск по загруженным документам (liteparse).

Entry-point модуль для `AgentBuilder.use_plugin(boba.tool.doc)` или
discovery через `[project.entry-points."boba.plugins"]`.

Tools (`[tool.doc]`, `BOBA_TOOL__DOC__*`):
- `read_document`        — весь текст документа;
- `read_document_window` — срез текста по символьному окну (стрим порциями);
- `read_pages`           — текст выбранных страниц (target_pages);
- `document_outline`     — постраничная карта (размер/символов/фрагментов);
- `search_document`      — поиск фразы с координатами и сниппетом.

Все шарят `DocPluginConfig`. `ProjectWorkspaceShell` инжектится через
`FromDI(Scope.APP)` — приложение обязано зарегистрировать provider'а.
"""

from __future__ import annotations

from boba.tool.doc.config import DocPluginConfig
from boba.tool.doc.outline import document_outline
from boba.tool.doc.pages import read_pages
from boba.tool.doc.read import read_document
from boba.tool.doc.search import search_document
from boba.tool.doc.window import read_document_window

__all__ = [
    "DocPluginConfig",
    "document_outline",
    "read_document",
    "read_document_window",
    "read_pages",
    "search_document",
]
