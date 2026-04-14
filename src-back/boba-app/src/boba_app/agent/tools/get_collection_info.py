"""Tool: информация о векторной коллекции."""

from __future__ import annotations

from typing import Iterator

from boba_domain.agent.events import DocPipelineEvent
from boba_domain.core.tools import EmptyParams, Tool, ToolOutput, ToolResult
from boba_domain.core.vectorstore import VectorStoreService
from boba_domain.workspace import Workspace
from boba_domain.di_types import CollectionName

DocToolOutput = ToolOutput[DocPipelineEvent]


class GetCollectionInfoTool(Tool[DocPipelineEvent, EmptyParams]):
    """Получение информации о векторной коллекции."""

    def __init__(
        self, ws: Workspace, vs: VectorStoreService, collection_name: CollectionName
    ) -> None:
        self._ws = ws
        self._vs = vs
        self._collection_name = collection_name

    @property
    def name(self) -> str:
        return "get_collection_info"

    @property
    def description(self) -> str:
        return (
            "Получить информацию о векторной коллекции: "
            "количество документов, модель эмбеддингов. "
            "Используй чтобы узнать состояние индекса."
        )

    @property
    def params_type(self) -> type[EmptyParams]:
        return EmptyParams

    def execute(self, params: EmptyParams) -> Iterator[DocToolOutput]:
        name = self._collection_name
        count = self._vs.collection_doc_count(name)
        model = self._vs.get_collection_embedding_model(name)

        info = (
            f"Коллекция: {name}\n"
            f"Документов (чанков): {count}\n"
            f"Модель эмбеддингов: {model or 'не задана'}"
        )

        if count == 0:
            info += (
                "\n\nВНИМАНИЕ: коллекция пуста, документы не проиндексированы. "
                "Вызови index_documents для индексации, затем search_documents для поиска."
            )

        yield ToolResult(content=info)
