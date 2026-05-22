"""Форматирование SQL-результата в markdown-таблицу для LLM.

Markdown table — компромисс между человекочитаемостью и token-эффективностью:
LLM-модели хорошо парсят `| col | col |` синтаксис, и output легко
переиспользуется в финальном ответе пользователю.

Безопасность для payload:
- Длинные значения обрезаются до `max_cell_chars` с суффиксом `…`.
- Бинарные blob'ы → `<bytes:N>` placeholder.
- NULL → явный `(null)`.
- Newlines и `|`/`\\` экранируются (иначе ломается markdown-rendering).
- Truncated-маркер `... ({k} more rows omitted)` если есть ещё.
"""

from __future__ import annotations

from typing import Any

__all__ = ["format_cell", "format_markdown_table"]


_NULL_REPR = "(null)"


def format_cell(value: Any, *, max_chars: int) -> str:
    """Стрингифицировать одно cell-значение под markdown-таблицу.

    NULL → `(null)`. bytes → `<bytes:N>` (хешировать смысла нет —
    placeholder инсайт о том, что binary). Остальное → str + escape +
    truncate.
    """
    if value is None:
        return _NULL_REPR
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<bytes:{len(value)}>"
    s = str(value)
    # Escape pipe-символа и backslash'а (ломают markdown-таблицу),
    # и схлопываем newlines (multi-row cell — ломаный rendering).
    s = s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ⏎ ")
    if len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s


def format_markdown_table(
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    max_cell_chars: int,
    truncated: bool = False,
    truncated_msg: str = "more rows omitted (увеличьте row_limit)",
) -> str:
    """Markdown-таблица. Пустой результат → одна строка-заглушка.

    Header → разделитель `|---|---|...|` → строки → опциональный
    truncated-маркер. Длина строк может выйти больше payload-лимита —
    caller отвечает за overall-cap (например, через row count).
    """
    if not columns:
        return "_(empty result: no columns)_"

    # Header.
    header_cells = [format_cell(c, max_chars=max_cell_chars) for c in columns]
    separator = ["---"] * len(columns)

    lines = [
        "| " + " | ".join(header_cells) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    if not rows:
        lines.append(
            "| " + " | ".join(["_(no rows)_"] + [""] * (len(columns) - 1)) + " |",
        )
    else:
        for row in rows:
            cells = [format_cell(v, max_chars=max_cell_chars) for v in row]
            # Если row короче (странный случай), допишем пустыми.
            cells.extend([""] * (len(columns) - len(cells)))
            lines.append("| " + " | ".join(cells[: len(columns)]) + " |")

    if truncated:
        lines.append(f"_... {truncated_msg}_")

    return "\n".join(lines)
