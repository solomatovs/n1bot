"""Tool: список доступных документов в папке."""
from __future__ import annotations

from typing import Iterator

from boba_app.readers.registry import DocumentReaderRegistry
from boba_domain.agent.events import DocPipelineEvent
from boba_domain.core.tools import EmptyParams, Tool, ToolOutput, ToolResult
from boba_domain.workspace import Workspace

DocToolOutput = ToolOutput[DocPipelineEvent]


class ListFilesTool(Tool[DocPipelineEvent, EmptyParams]):
    """Список доступных документов в папке."""

    def __init__(self, ws: Workspace, reader_registry: DocumentReaderRegistry) -> None:
        self._ws = ws
        self._reader_registry = reader_registry

    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return (
            "Показать список доступных документов в папке. "
            "Используй, когда нужно узнать какие файлы есть."
        )

    @property
    def params_type(self) -> type[EmptyParams]:
        return EmptyParams

    def execute(self, params: EmptyParams) -> Iterator[DocToolOutput]:
        lines = []
        for f in self._reader_registry.iter_files(self._ws.folder_path):
            lines.append(f"- {f.name}")

        if not lines:
            yield ToolResult(content="В папке нет поддерживаемых документов.")
            return

        yield ToolResult(
            content=f"Доступные документы ({len(lines)}):\n" + "\n".join(lines),
        )
