"""FsRequest — DTO для FS transport: путь к файлу + source_id + metadata."""

from __future__ import annotations

from dataclasses import dataclass, field

from boba.indexing import Metadata, SourceId

__all__ = ["FsRequest"]


@dataclass(frozen=True)
class FsRequest:
    """План open(file)

    source_id — caller-supplied canonical id (RequestSource ставит,
    Transport исполняет — Transport не формирует identity).
    metadata — обогащение для Section.metadata (relative_path,
    space_key для FS-export Confluence и т.п.). Transport может
    добавить свои ключи (mtime, size).
    """

    path: str
    source_id: SourceId
    metadata: Metadata = field(default_factory=Metadata.empty)
