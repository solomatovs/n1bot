"""Shared helpers for agent tools."""

from __future__ import annotations

from boba_domain.search.types import ChunkLocation
from boba_domain.errors import CorruptedIndexError

MAX_RESULT_CHARS = 4000

_REQUIRED_META_FIELDS = (
    "source_file",
    "start_line",
    "end_line",
    "start_offset",
    "end_offset",
)


def parse_location(meta: dict) -> ChunkLocation:
    """Валидировать метаданные и собрать ChunkLocation."""
    missing = [f for f in _REQUIRED_META_FIELDS if f not in meta]
    if missing:
        raise CorruptedIndexError(
            missing_fields=missing,
            source_file=meta.get("source_file", ""),
        )
    return ChunkLocation(
        source_file=meta["source_file"],
        start_line=meta["start_line"],
        end_line=meta["end_line"],
        start_offset=meta["start_offset"],
        end_offset=meta["end_offset"],
        section_title=meta.get("section_title", ""),
    )


def read_line_range(file_path, start_line: int, end_line: int) -> str:
    """Прочитать диапазон строк из файла (1-based)."""
    lines: list[str] = []
    with open(file_path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if i > end_line:
                break
            if i >= start_line:
                lines.append(line.rstrip("\n"))
    return "\n".join(lines)
