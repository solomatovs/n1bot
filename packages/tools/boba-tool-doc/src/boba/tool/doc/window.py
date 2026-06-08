"""Tool: чтение документа символьным окном [start_char, start_char+length)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.doc._engine import DocEngine
from boba.tool.doc.config import DocPluginConfig
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.tools.domain import TextResult
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["read_document_window"]


@tool
def read_document_window(
    path: Annotated[
        str,
        Field(min_length=1, description="Путь к документу в workspace."),
    ],
    start_char: Annotated[
        int,
        Field(ge=0, description="Смещение начала окна в символах, 0-based."),
    ],
    length: Annotated[
        int,
        Field(ge=1, description="Сколько символов вернуть начиная со start_char."),
    ],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[DocPluginConfig, FromConfig()],
) -> TextResult:
    """Вернуть срез текста документа [start_char, start_char+length).

    Для последовательного чтения большого документа порциями, независимо от
    страниц: листай, увеличивая start_char на длину прочитанного, пока
    metadata.has_more == 'True'. Окно не шире max_text_chars.
    Фактический end_char, total_chars и has_more — в metadata.
    """
    max_window = cfg.max_text_chars
    if length > max_window:
        raise RuntimeError(
            f"length ({length}) шире лимита {max_window}. Читай окнами "
            f"≤ {max_window} символов: start_char={start_char}, length={max_window}.",
        )

    data = DocEngine.read_bytes(shell, path)
    result = DocEngine.parse(cfg, data, path)
    chunk, end_char, total, has_more = DocEngine.window(
        result.text, start_char, length,
    )

    return TextResult(
        text=chunk,
        metadata={
            "path": path,
            "start_char": str(start_char),
            "end_char": str(end_char),
            "total_chars": str(total),
            "has_more": str(has_more),
        },
    )
