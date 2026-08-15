"""Grep по строкам текста: компиляция шаблона, контекст, обрезка, сводка.

Ошибки: своих не выпускает; битый regex деградирует в литеральный поиск.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator
from typing import Any

__all__ = ["TextGrep"]


class TextGrep:
    """Поиск совпадений шаблона в строках текста."""

    @staticmethod
    def compile_pattern(
        pattern: str, *, fixed_string: bool, case_insensitive: bool
    ) -> re.Pattern[str]:
        flags = re.IGNORECASE if case_insensitive else 0
        if fixed_string:
            return re.compile(re.escape(pattern), flags)
        try:
            return re.compile(pattern, flags)
        except re.error:
            return re.compile(re.escape(pattern), flags)

    @staticmethod
    def iter_matches(
        text: str, compiled: re.Pattern[str], *, context: int
    ) -> Iterator[dict[str, Any]]:
        before: deque[str] = deque(maxlen=context if context > 0 else 0)
        after_needed: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            for row in after_needed:
                if len(row["after"]) < context:
                    row["after"].append(line)
            ready: list[dict[str, Any]] = []
            for row in after_needed:
                if len(row["after"]) >= context:
                    ready.append(row)
            for row in ready:
                after_needed.remove(row)
                yield row
            if compiled.search(line):
                row = {
                    "line": number,
                    "content": line,
                    "before": list(before),
                    "after": [],
                }
                if context > 0:
                    after_needed.append(row)
                else:
                    yield row
            before.append(line)
        for row in after_needed:
            yield row

    @staticmethod
    def clip_row(row: dict[str, Any], limit: int) -> dict[str, Any]:
        before: list[str] = []
        for line in row["before"]:
            before.append(line[:limit])
        after: list[str] = []
        for line in row["after"]:
            after.append(line[:limit])
        return {
            "line": row["line"],
            "content": row["content"][:limit],
            "before": before,
            "after": after,
        }

    @staticmethod
    def note(source: str, rows: list[dict[str, Any]], *, limit: int) -> str:
        if not rows:
            return f"{source}: no matches found"
        parts = [source, f"matches: {len(rows)}"]
        if len(rows) >= limit:
            parts.append(f"showing first {len(rows)} (more found)")
        return "; ".join(parts)
