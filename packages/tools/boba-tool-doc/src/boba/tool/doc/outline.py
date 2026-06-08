"""Tool: карта документа — постраничная структура (liteparse)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.doc._engine import DocEngine
from boba.tool.doc.config import DocPluginConfig
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.tools.domain import TableResult
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["document_outline"]


@tool
def document_outline(
    path: Annotated[
        str,
        Field(min_length=1, description="Путь к документу в workspace."),
    ],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[DocPluginConfig, FromConfig()],
) -> TableResult:
    """Вернуть карту документа: по строке на страницу (размер, символов, фрагментов).

    Дешёвый обзор перед чтением: по нему выбирают нужные страницы для
    read_pages. Сам текст не возвращается.
    """
    data = DocEngine.read_bytes(shell, path)
    result = DocEngine.parse(cfg, data, path)

    rows = [
        {
            "page": page.page_num,
            "width": round(page.width, 1),
            "height": round(page.height, 1),
            "chars": len(page.text),
            "items": len(page.text_items),
        }
        for page in result.pages
    ]
    return TableResult(
        rows=rows,
        note=f"{path}: страниц {result.num_pages}",
        metadata={"path": path},
    )
