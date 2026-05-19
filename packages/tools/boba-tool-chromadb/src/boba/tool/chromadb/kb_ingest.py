"""Tool: индексация заранее настроенной оператором папки `.md` в коллекцию.

LLM-facing wrapper над `MdFolderIndexer`. Оператор закрепляет folder и
collection за собой через `[tool.chromadb]` (поля `ingest_folder` /
`ingest_collection` / `ingest_collection_description`) — LLM не выбирает,
во что и откуда индексировать, только опционально включает `prune_missing`.

Всё, кроме самого tool'а — `Embedder[str]`, chromadb client, парсер,
ChromaVectorStore, MdFolderIndexer — собирается Dishka-контейнером по
графу зависимостей в `di.ChromadbProvider`. Tool достаёт верхушку
графа (`MdFolderIndexer`) в `execute()` и не знает про слои.

`container.get(MdFolderIndexer)` ленив: при первом execute Dishka
рекурсивно собирает graph и кеширует на `Scope.APP`. Последующие
execute получают тот же indexer без работы.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dishka import Container, make_container
from pydantic import BaseModel, ConfigDict, Field

from boba.indexing.context import CollectionId
from boba.plugin.prompt import PromptOverlay
from boba.tool.chromadb.embedder_factory import EmbeddingModelNotConfiguredError
from boba.tool.chromadb.md_folder_ingest import MdFolderIndexer
from boba.tools.domain import (
    ErrorResult,
    JsonResult,
    Tool,
    ToolContext,
    ToolResult,
    ToolSourceId,
)

__all__ = ["KbIngestArgs", "KbIngestTool", "KbIngestToolConfig"]


class KbIngestArgs(BaseModel):
    """Проиндексировать заранее настроенную оператором папку `.md` файлов.

    Папка и имя коллекции зашиты в `[tool.chromadb]` оператором — LLM
    не выбирает (защита от случайного индексирования чужих файлов и
    создания мусорных коллекций). LLM управляет только cleanup-семантикой.

    Возвращает JSON: {folder, collection, indexed, skipped_unchanged,
    pruned, failed: [{path, error}]}.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prune_missing: bool = Field(
        default=False,
        description=(
            "Если true, удалить из коллекции чанки, чьих source_id нет "
            "среди индексируемых файлов (cleanup удалённых документов)."
        ),
    )


@dataclass(frozen=True)
class KbIngestToolConfig:
    """DTO tool'а: все cfg-поля, которые провайдер использует для сборки графа."""

    persist_path: str
    embedding_model: str
    embedding_base_url: str
    embedding_api_key: str
    ingest_folder: str
    ingest_collection: str
    ingest_collection_description: str
    prompt: PromptOverlay


class KbIngestTool(Tool[KbIngestArgs, KbIngestToolConfig]):
    """Индексирует pre-настроенную оператором папку в pre-настроенную коллекцию.

    Конструктор `(cfg, ctx, source_id)` — никаких extra-параметров.
    Внутри только Dishka-контейнер: всё остальное (parser, embedder,
    client, store, indexer) приходит из него через `container.get(...)`
    в `execute()`.
    """

    def __init__(
        self,
        cfg: KbIngestToolConfig,
        ctx: Any,
        source_id: ToolSourceId,
    ) -> None:
        super().__init__(cfg, ctx, source_id)
        # Локальный импорт чтобы разорвать циркуляцию kb_ingest ↔ di
        # (di.py тащит `KbIngestToolConfig` из этого модуля).
        from boba.tool.chromadb.di import (  # noqa: PLC0415
            ChromadbProvider,
        )

        self._container: Container = make_container(
            ChromadbProvider(),
            context={KbIngestToolConfig: cfg},
        )

    def execute(self, ctx: ToolContext, req: KbIngestArgs) -> ToolResult:
        del ctx

        try:
            indexer = self._container.get(MdFolderIndexer)
        except EmbeddingModelNotConfiguredError as e:
            return ErrorResult(
                message=str(e),
                error_kind="embedding_model_not_configured",
            )

        try:
            stats = indexer.index(
                folder=Path(self._cfg.ingest_folder),
                collection=CollectionId(self._cfg.ingest_collection),
                collection_description=(
                    self._cfg.ingest_collection_description or None
                ),
                prune_missing=req.prune_missing,
            )
        except FileNotFoundError as e:
            return ErrorResult(message=str(e), error_kind="folder_not_found")
        except NotADirectoryError as e:
            return ErrorResult(
                message=str(e), error_kind="folder_not_a_directory",
            )

        return JsonResult(
            payload={
                "folder": stats.folder,
                "collection": stats.collection,
                "indexed": stats.indexed,
                "skipped_unchanged": stats.skipped_unchanged,
                "pruned": stats.pruned,
                "failed": [
                    {"path": f.path, "error": f.error} for f in stats.failed
                ],
            },
        )
