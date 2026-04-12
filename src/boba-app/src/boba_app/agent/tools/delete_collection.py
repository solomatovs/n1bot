"""Tool: удаление векторной коллекции."""

from __future__ import annotations

from typing import Iterator

from boba_domain.agent.events import DocPipelineEvent
from boba_domain.core.tools import EmptyParams, Tool, ToolOutput, ToolResult
from boba_domain.core.vectorstore import VectorStoreService
from boba_domain.workspace import Workspace
from boba_domain.di_types import CollectionName

DocToolOutput = ToolOutput[DocPipelineEvent]


class DeleteCollectionTool(Tool[DocPipelineEvent, EmptyParams]):
    """Удаление векторной коллекции для переиндексации."""

    def __init__(
        self, ws: Workspace, vs: VectorStoreService, collection_name: CollectionName
    ) -> None:
        self._ws = ws
        self._vs = vs
        self._collection_name = collection_name

    @property
    def name(self) -> str:
        return "delete_collection"

    @property
    def description(self) -> str:
        return (
            "Удалить векторную коллекцию документов. "
            "Используй перед переиндексацией, если нужно полностью пересоздать индекс. "
            "После удаления выполни index_documents."
        )

    @property
    def params_type(self) -> type[EmptyParams]:
        return EmptyParams

    def execute(self, params: EmptyParams) -> Iterator[DocToolOutput]:
        name = self._collection_name
        count = self._vs.collection_doc_count(name)

        if count == 0:
            yield ToolResult(content=f"Коллекция '{name}' пуста или не существует.")
            return

        self._vs.remove_collection(name)
        yield ToolResult(content=f"Коллекция '{name}' удалена ({count} документов).")
