"""FsRequest — DTO для FS transport: путь к файлу + metadata."""

from __future__ import annotations

from dataclasses import dataclass, field

from boba.indexing import Metadata

__all__ = ["FsRequest"]


@dataclass(frozen=True)
class FsRequest:
    """План open(file)

    source_id (идентичность) НЕ часть запроса — её выводит транспорт из path
    (FsTransport: `fs:{path}`; WorkspaceTransport: `ws:{wid}:{path}`).
    metadata — обогащение для Section.metadata (relative_path,
    space_key для FS-export Confluence и т.п.). Transport может
    добавить свои ключи (mtime, size).
    """

    path: str
    metadata: Metadata = field(default_factory=Metadata.empty)
