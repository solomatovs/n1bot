"""Tool: чтение текста выбранных страниц документа (liteparse target_pages)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.doc._engine import DocEngine
from boba.tool.doc.config import DocPluginConfig
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.tools.domain import TextResult
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["read_pages"]


@tool
def read_pages(
    path: Annotated[
        str,
        Field(min_length=1, description="Путь к документу в workspace."),
    ],
    pages: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Страницы для чтения, 1-based: диапазоны и перечисление "
                "через запятую, например '1-5,10,15-20'."
            ),
        ),
    ],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[DocPluginConfig, FromConfig()],
) -> TextResult:
    """Распарсить только указанные страницы и вернуть их текст.

    Дешевле `read_document` для больших PDF: парсятся лишь нужные страницы.
    Фактически разобранные номера страниц и факт обрезки — в `metadata`.
    """
    data = DocEngine.read_bytes(shell, path)
    result = DocEngine.parse(cfg, data, path, target_pages=pages)

    text, truncated = DocEngine.clip(result.text, cfg.max_text_chars)
    if truncated:
        text += f"\n\n[обрезано до {cfg.max_text_chars} символов]"

    page_nums = ",".join(str(p.page_num) for p in result.pages)
    return TextResult(
        text=text,
        metadata={
            "path": path,
            "pages": page_nums,
            "truncated": str(truncated),
        },
    )
