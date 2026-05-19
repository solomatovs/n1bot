"""Tool: чтение содержимого файла (целиком или диапазон строк)."""

from __future__ import annotations

from io import TextIOBase
from typing import Annotated, Any

from pydantic import Field

from boba.tool.files.config import FilesPluginConfig
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["cat"]


@tool
def cat(  # noqa: PLR0913
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[FilesPluginConfig, FromConfig()],
    path: Annotated[str, Field(min_length=1, description="Путь к файлу.")],
    start_line: Annotated[
        int,
        Field(ge=1, description="Первая строка окна. 1 = начало файла."),
    ],
    end_line: Annotated[
        int,
        Field(ge=1, description="Последняя строка окна, включительно >= start_line."),
    ],
    encoding: Annotated[
        str,
        Field(min_length=1, description="Кодировка файла. По умолчанию 'utf-8'."),
    ] = "utf-8",
) -> dict[str, Any]:
    """Чтение содержимого файла (диапазон строк 1-based, включительно).

    Запрашивай окнами не шире `cat_max_lines` из конфига (default 2000).
    """
    if start_line > end_line:
        raise RuntimeError(
            f"start_line ({start_line}) должна быть <= end_line ({end_line})",
        )
    max_lines = cfg.cat_max_lines
    if end_line - start_line + 1 > max_lines:
        raise RuntimeError(
            f"Запрошенный диапазон {start_line}-{end_line} шире лимита "
            f"{max_lines}. Читай окнами ≤ {max_lines} строк: "
            f"start_line={start_line}, end_line={start_line + max_lines - 1}.",
        )

    try:
        with shell.read_text(path, encoding) as f:
            text, last = _read_range(f, start_line, end_line)
    except WorkspaceNotFoundError as e:
        raise RuntimeError(f"Файл не найден: {path}") from e
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка чтения: {e}") from e

    return {
        "path": path,
        "start_line": start_line,
        "end_line": last,
        "content": text,
    }


def _read_range(f: TextIOBase, start: int, end: int) -> tuple[str, int]:
    """Стримит файл построчно, возвращая только строки [start, end]."""
    collected: list[str] = []
    last = start - 1
    for i, line in enumerate(f, start=1):
        if i < start:
            continue
        if i > end:
            break
        collected.append(line.rstrip("\r\n"))
        last = i
    return "\n".join(collected), last
