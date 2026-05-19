"""Tool: индексация заранее настроенной оператором папки `.md` в коллекцию.

LLM-facing wrapper над `MdFolderIndexer`. Оператор закрепляет folder и
collection за собой через `[tool.chromadb]` (поля `ingest_folder` /
`ingest_collection` / `ingest_collection_description`) — LLM не выбирает,
во что и откуда индексировать, только опционально включает `prune_missing`.

В v2 граф зависимостей (Embedder, ClientAPI, MdChunkParser,
ChromaVectorStore, MdFolderIndexer) живёт в общем DI-Container'е агента
и собирается лениво — при первом резолве `MdFolderIndexer`. Tool сам
ничего не строит.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import CollectionId
from boba.tool.chromadb.config import ChromadbPluginConfig
from boba.tool.chromadb.enable import chromadb_enable_if
from boba.tool.chromadb.md_folder_ingest import MdFolderIndexer
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["KbIngestTool"]


@tool(enable_if=chromadb_enable_if("kb_ingest"))
class KbIngestTool:
    """Индексирует pre-настроенную оператором папку в pre-настроенную коллекцию.

    Возвращает JSON: {folder, collection, indexed, skipped_unchanged,
    pruned, failed: [{path, error}]}.
    """

    def __call__(
        self,
        indexer: Annotated[MdFolderIndexer, FromDI(Scope.APP)],
        cfg: Annotated[ChromadbPluginConfig, FromConfig()],
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
