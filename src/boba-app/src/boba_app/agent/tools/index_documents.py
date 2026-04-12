"""Tool: индексация документов для последующего поиска."""
from __future__ import annotations

from typing import Any, Dict, Iterator

from boba_domain.workspace import Workspace
from boba_domain.di_types import CollectionName, EmbeddingModel
from boba_domain.agent.events import (
    DocPipelineEvent,
    FileIndexed,
    IndexingDone,
    IndexingSkipped,
)
from boba_app.index_pipeline import (
    IndexContext,
    run_indexing,
)
from boba_app.index_pipeline import (
    FileCompleted as IdxFileCompleted,
    IndexingDone as IdxDone,
    IndexingSkipped as IdxSkipped,
)
from boba_app.readers.registry import DocumentReaderRegistry
from boba_domain.core.tools import EmptyParams, Tool, ToolEvent, ToolOutput, ToolResult
from boba_domain.core.vectorstore import VectorStoreService

DocToolOutput = ToolOutput[DocPipelineEvent]


class IndexDocumentsTool(Tool[DocPipelineEvent, EmptyParams]):
    """Индексация документов для последующего поиска."""

    def __init__(
        self,
        ws: Workspace,
        vs: VectorStoreService,
        collection_name: CollectionName,
        embedding_model: EmbeddingModel,
        reader_registry: DocumentReaderRegistry,
    ) -> None:
        self._ws = ws
        self._vs = vs
        self._collection_name = collection_name
        self._embedding_model = embedding_model
        self._reader_registry = reader_registry

    @property
    def name(self) -> str:
        return "index_documents"

    @property
    def description(self) -> str:
        return (
            "Индексация документов для последующего поиска. "
            "Вызови ПЕРЕД search_documents, если документы ещё не проиндексированы "
            "или если search_documents ничего не находит. "
            "Индексация проверяет изменения — если документы не менялись, пропускает."
        )

    @property
    def params_type(self) -> type[EmptyParams]:
        return EmptyParams

    def execute(self, params: EmptyParams) -> Iterator[DocToolOutput]:
        idx_ctx = IndexContext(
            source_path=self._ws.folder_path,
            manifest_path=self._ws.manifest_path,
            collection_name=self._collection_name,
            embedding_model=self._embedding_model,
            vectorstore_service=self._vs,
            reader_registry=self._reader_registry,
        )

        total_files = 0
        total_chunks = 0

        for event in run_indexing(idx_ctx):
            match event:
                case IdxSkipped(collection=c, doc_count=n):
                    yield ToolEvent(IndexingSkipped(collection=c, doc_count=n))
                    yield ToolResult(content=f"Индекс актуален ({n} чанков), переиндексация не нужна.")
                    return

                case IdxFileCompleted(filename=f, chunks=c, index=i, total=t):
                    total_files = i
                    total_chunks += c
                    yield ToolEvent(FileIndexed(filename=f, chunks=c, index=i, total=t))

                case IdxDone(total_files=f, total_chunks=c):
                    total_files = f
                    total_chunks = c
                    yield ToolEvent(IndexingDone(total_files=f, total_chunks=c))

        yield ToolResult(content=f"Индексация завершена: {total_files} файлов, {total_chunks} чанков.")
