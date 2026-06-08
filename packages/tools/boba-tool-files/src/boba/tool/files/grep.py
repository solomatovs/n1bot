"""Tool: поиск по содержимому файлов (grep-like)."""

from __future__ import annotations

from itertools import islice
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

__all__ = ["grep"]


@tool
def grep(  # noqa: PLR0913
    pattern: Annotated[
        str,
        Field(min_length=1, description="Python-regex; литерал при fixed_string=true."),
    ],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[FilesPluginConfig, FromConfig()],
    encoding: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Кодировка читаемых файлов: 'utf-8', 'cp1251', 'latin-1', и т.п. "
                "Файлы, не декодируемые этой кодировкой, пропускаются."
            ),
        ),
    ],
    path: Annotated[
        str | None,
        Field(min_length=1, description="Стартовый путь. Без значения — cwd."),
    ] = None,
    recursive: Annotated[
        bool,
        Field(description="Рекурсивный обход директории. По умолчанию true."),
    ] = True,
    include: Annotated[
        str | None,
        Field(
            min_length=1,
            description="Fnmatch-glob по пути (например '*.py'). Без значения — все.",
        ),
    ] = None,
    case_insensitive: Annotated[
        bool,
        Field(description="Игнорировать регистр. По умолчанию false."),
    ] = False,
    context: Annotated[
        int,
        Field(ge=0, description="Строк контекста до и после каждого совпадения."),
    ] = 0,
    limit: Annotated[
        int,
        Field(ge=1, description="Максимум совпадений в ответе. По умолчанию 100."),
    ] = 100,
    fixed_string: Annotated[
        bool,
        Field(description="Литеральный поиск без regex. По умолчанию false."),
    ] = False,
) -> TableResult:
    """Найти совпадения pattern в текстовых файлах.

    Возвращает TableResult — таблицу matches с колонками path/line/
    content/before/after (+truncated_lines на усечённых). Бинарные и
    недекодируемые файлы пропускаются. Переполнение limit и обрезка длинных
    строк по max_text_chars — в note.
    """
    try:
        iterator = shell.grep(
            pattern,
            path,
            recursive=recursive,
            include=include,
            case_insensitive=case_insensitive,
            context=context,
            limit=limit,
            fixed_string=fixed_string,
            encoding=encoding,
        )
        matches = list(islice(iterator, limit + 1))
    except WorkspaceNotFoundError as e:
        raise RuntimeError(f"Путь не найден: {path}") from e
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка grep: {e}") from e
    except LookupError as e:
        raise RuntimeError(
            f"Неизвестная кодировка '{encoding}'. Используй валидную "
            f"Python-кодировку (utf-8, cp1251, latin-1, ...).",
        ) from e

    total = len(matches)
    truncated = total > limit
    if truncated:
        matches = matches[:limit]

    max_chars = cfg.max_text_chars
    items: list[dict[str, Any]] = []
    for m in matches:
        content, cut_c = _clip(m.content, max_chars)
        before, cut_b = _clip_many(m.before, max_chars)
        after, cut_a = _clip_many(m.after, max_chars)
        item: dict[str, Any] = {
            "path": m.path,
            "line": m.line,
            "content": content,
            "before": before,
            "after": after,
        }
        if cut_c or cut_b or cut_a:
            item["truncated_lines"] = True
        items.append(item)

    if not items:
        note = "совпадений не найдено"
    else:
        parts = [f"совпадений: {len(items)}"]
        if truncated:
            parts.append(f"показаны первые {limit} (найдено больше)")
        if any(it.get("truncated_lines") for it in items):
            parts.append(f"строки усечены до {max_chars} символов")
        note = "; ".join(parts)
    return TableResult(rows=items, note=note)


def _clip(s: str, limit: int) -> tuple[str, bool]:
    """Обрезает строку до limit символов; возвращает (строка, был_ли_обрезан)."""
    if len(s) <= limit:
        return s, False
    return s[:limit], True


def _clip_many(
    lines: list[str] | tuple[str, ...], limit: int
) -> tuple[list[str], bool]:
    """Применяет _clip к каждой строке списка."""
    out: list[str] = []
    cut = False
    for line in lines:
        clipped, c = _clip(line, limit)
        out.append(clipped)
        cut = cut or c
    return out, cut
