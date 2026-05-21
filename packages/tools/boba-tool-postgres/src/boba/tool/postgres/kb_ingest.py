"""Tool: индексация заранее настроенной оператором папки в коллекцию.

LLM-facing wrapper над `FolderIndexer`. Оператор закрепляет folder и
collection за собой через `[tool.postgres]` (поля `ingest_folder` /
`ingest_collection` / `ingest_collection_description`) — LLM не выбирает,
во что и откуда индексировать, только опционально включает `prune_missing`.

Поддерживаемые форматы (диспатч по расширению):
- `.md`         → `MdChunkParser` (один файл = один чанк, pre-chunked)
- `.html/.htm`  → `HtmlChunkParser` (auto-split по H1/H2)

Граф зависимостей (Embedder, ConnectionPool, MdChunkParser, HtmlChunkParser,
PostgresVectorStore, FolderIndexer) живёт в общем DI-Container'е и
собирается лениво — при первом резолве `FolderIndexer`. Tool сам ничего
не строит.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import CollectionId
from boba.tool.postgres.config import PostgresPluginConfig
from boba.tool.postgres.folder_indexer import FolderIndexer
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["kb_ingest"]


@tool
def kb_ingest(
    indexer: Annotated[FolderIndexer, FromDI(Scope.APP)],
    cfg: Annotated[PostgresPluginConfig, FromConfig()],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id "
                "нет среди индексируемых файлов (cleanup удалённых документов)."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует pre-настроенную оператором папку в pre-настроенную коллекцию.

    Возвращает JSON: {folder, collection, indexed, skipped_unchanged,
    pruned, failed: [{path, error}]}.
    """
    try:
        stats = indexer.index(
            folder=Path(cfg.ingest_folder),
            collection=CollectionId(cfg.ingest_collection),
            collection_description=(
                cfg.ingest_collection_description or None
            ),
            prune_missing=prune_missing,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"folder_not_found: {e}") from e
    except NotADirectoryError as e:
        raise RuntimeError(f"folder_not_a_directory: {e}") from e

    return {
        "folder": stats.folder,
        "collection": stats.collection,
        "indexed": stats.indexed,
        "skipped_unchanged": stats.skipped_unchanged,
        "pruned": stats.pruned,
        "failed": [
            {"path": f.path, "error": f.error} for f in stats.failed
        ],
    }
