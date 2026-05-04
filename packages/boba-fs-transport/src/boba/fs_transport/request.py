"""FsRequest: DTO для FsTransport — путь к файлу + identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["FsRequest"]


@dataclass(frozen=True)
class FsRequest:
    """План open(file). Только path; auth/headers неприменимы для FS.

    `source_id` — RequestSource ставит canonical (например `fs:/abs/path`
    или другой схемой, если файл — это нечто known-канонически). Если пуст,
    Transport заполнит как `fs:/abs/path` (resolve'ит path).

    `metadata` — обогащение для Section.metadata (например relative_path,
    space_key для FS-export Confluence и т.п.).
    """

    path: str
    source_id: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)
