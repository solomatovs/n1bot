"""Tool: чтение содержимого файла (целиком или диапазон строк)."""

from __future__ import annotations

from io import TextIOBase
from typing import Annotated, Any

from pydantic import Field

from boba.tool.files.config import FilesPluginConfig
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.tools.domain import TableResult
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
        Field(
            min_length=1,
            description=(
                "Кодировка файла: 'utf-8', 'cp1251', 'latin-1', и т.п. "
                "Для бинарных файлов используй read_bytes."
            ),
        ),
    ],
) -> TableResult:
    """Чтение содержимого файла (диапазон строк 1-based, включительно).

    Возвращает TableResult — таблицу строк с колонками line/content.
    Путь, фактический диапазон и обрезка — в note/metadata. Запрашивай
    окнами не шире cat_max_lines из конфига (default 2000); контент
    дополнительно ограничен max_text_chars (default 200_000).
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
            rows, last, truncated = _read_range(
                f, start_line, end_line, cfg.max_text_chars,
            )
    except WorkspaceNotFoundError as e:
        raise RuntimeError(f"Файл не найден: {path}") from e
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка чтения: {e}") from e
    except LookupError as e:
        raise RuntimeError(
            f"Неизвестная кодировка '{encoding}'. Используй валидную "
            f"Python-кодировку (utf-8, cp1251, latin-1, ...).",
        ) from e
    except UnicodeDecodeError as e:
        raise RuntimeError(
            f"Не удалось декодировать {path} как '{encoding}' "
            f"(позиция {e.start}): {e.reason}. Похоже, файл бинарный или в "
            f"другой кодировке: для архивов (docx/xlsx/zip) используй unzip, "
            f"для произвольных байтов — read_bytes с encoding='hex'/'base64'.",
        ) from e

    parts = [f"{path}:{start_line}-{last}"]
    if truncated:
        parts.append(f"обрезано до {cfg.max_text_chars} символов")
    return TableResult(
        rows=rows,
        note="; ".join(parts),
        metadata={"path": path},
    )


def _read_range(
    f: TextIOBase, start: int, end: int, max_chars: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Стримит файл, возвращая строки [start, end] ({line, content}) с обрезкой."""
    rows: list[dict[str, Any]] = []
    last = start - 1
    total = 0
    truncated = False
    for i, line in enumerate(f, start=1):
        if i < start:
            continue
        if i > end:
            break
        chunk = line.rstrip("\r\n")
        sep = 1 if rows else 0
        remaining = max_chars - total - sep
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            rows.append({"line": i, "content": chunk[:remaining]})
            total += sep + remaining
            last = i
            truncated = True
            break
        rows.append({"line": i, "content": chunk})
        total += sep + len(chunk)
        last = i
    return rows, last, truncated
