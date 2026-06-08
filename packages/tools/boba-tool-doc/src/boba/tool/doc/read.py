"""Tool: парсинг загруженного документа в читаемый текст (liteparse)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.doc._engine import DocEngine
from boba.tool.doc.config import DocPluginConfig
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.tools.domain import TextResult
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["read_document"]


@tool
def read_document(
    path: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Путь к локальному файлу в workspace "
                "(PDF/docx/pptx/xlsx/изображение). НЕ URL: "
                "для web-страниц используй web_fetch."
            ),
        ),
    ],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[DocPluginConfig, FromConfig()],
) -> TextResult:
    """Распарсить документ из workspace и вернуть весь извлечённый текст.

    Текст обрезается до max_text_chars (default 200_000); число страниц
    и факт обрезки — в metadata. Для выбора конкретных страниц используй
    read_pages, для обзора структуры — document_outline.
    """
    data = DocEngine.read_bytes(shell, path)
    result = DocEngine.parse(cfg, data, path)

    text, truncated = DocEngine.clip(result.text, cfg.max_text_chars)
    if truncated:
        text += f"\n\n[обрезано до {cfg.max_text_chars} символов]"

    return TextResult(
        text=text,
        metadata={
            "path": path,
            "pages": str(result.num_pages),
            "truncated": str(truncated),
        },
    )
