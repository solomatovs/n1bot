"""Tool: индексация заранее настроенной оператором папки `.md` в коллекцию.

LLM-facing wrapper над `MdFolderIndexer`. Оператор закрепляет folder и
collection за собой через `[tool.chromadb]` (поля `ingest_folder` /
`ingest_collection` / `ingest_collection_description`) — LLM не выбирает,
во что и откуда индексировать, только опционально включает `prune_missing`.

`Embedder[str]` инжектится снаружи (через DI: `ExtensionContext` →
`EmbedderFactory` → resolve в Plugin.build). Tool принимает уже готовый
`Embedder` либо `None` (если оператор не сконфигурировал модель). При
попытке вызова в no-embedder режиме — fail-fast с понятным сообщением.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from boba.indexing.context import CollectionId
from boba.indexing.embedder import Embedder
from boba.plugin.prompt import PromptOverlay
from boba.tool.chromadb.md_folder_ingest import MdFolderIndexer
from boba.tool.chromadb.vector_store import ChromaVectorStore
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
    """DTO tool'а: pre-resolved параметры из `ChromadbPluginConfig`.

    embedding_* сюда не входят — `Embedder[str]` уже резолвлен снаружи
    (DI через `ExtensionContext`) и передан как отдельный конструкторный
    параметр. Tool остаётся «чистым»: не знает про factory.
    """

    ingest_folder: str
    ingest_collection: str
    ingest_collection_description: str
    prompt: PromptOverlay


class KbIngestTool(Tool[KbIngestArgs, KbIngestToolConfig]):
    """Индексирует pre-настроенную оператором папку в pre-настроенную коллекцию.

    Зависимости (передаются в конструктор готовыми):

    - `client` — chromadb `ClientAPI`, тот же что у read-side `kb_search`
      (один PersistentClient на persist_path, иначе file-lock contention).
    - `embedder` — уже построенный `Embedder[str]` либо `None`. None
      означает, что оператор не задал `embedding_model` — execute вернёт
      ErrorResult без попытки построить store.

    `ChromaVectorStore` и `MdFolderIndexer` собираются lazily при первом
    execute (это просто in-memory объекты, ничего не делают до upsert'а).
    """

    def __init__(
        self,
        client: Any,
        embedder: Embedder[str] | None,
        cfg: KbIngestToolConfig,
        ctx: Any,
        source_id: ToolSourceId,
    ) -> None:
        super().__init__(cfg, ctx, source_id)
        self._client = client
        self._embedder = embedder
        self._indexer: MdFolderIndexer | None = None

    def execute(self, ctx: ToolContext, req: KbIngestArgs) -> ToolResult:
        del ctx
        if not self._cfg.ingest_folder:
            return ErrorResult(
                message=(
                    "ingest_folder не задан в [tool.chromadb]; "
                    "kb_ingest требует pre-configured папку (LLM не выбирает)."
                ),
                error_kind="ingest_folder_not_configured",
            )
        if self._embedder is None:
            return ErrorResult(
                message=(
                    "embedding_model не сконфигурирован в [tool.chromadb]; "
                    "kb_ingest требует явный выбор модели "
                    "(или 'default' для built-in ONNX)."
                ),
                error_kind="embedding_model_not_configured",
            )

        try:
            stats = self._get_or_build_indexer().index(
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

    def _get_or_build_indexer(self) -> MdFolderIndexer:
        if self._indexer is None:
            assert self._embedder is not None  # проверено в execute
            store = ChromaVectorStore(
                client=self._client, embedder=self._embedder,
            )
            self._indexer = MdFolderIndexer(store=store)
        return self._indexer
