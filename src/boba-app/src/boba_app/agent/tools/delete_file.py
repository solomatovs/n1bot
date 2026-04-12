"""Tool: удаление файла из рабочей папки."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator

from boba_domain.agent.events import DocPipelineEvent
from boba_domain.core.tools import Tool, ToolOutput, ToolResult
from boba_domain.workspace import Workspace

DocToolOutput = ToolOutput[DocPipelineEvent]


@dataclass(frozen=True)
class DeleteFileParams:
    """Параметры удаления файла."""
    filename: str = field(metadata={"description": "Имя файла для удаления"})


class DeleteFileTool(Tool[DocPipelineEvent, DeleteFileParams]):
    """Удаление файла из рабочей папки."""

    def __init__(self, ws: Workspace) -> None:
        self._ws = ws

    @property
    def name(self) -> str:
        return "delete_file"

    @property
    def description(self) -> str:
        return (
            "Удалить файл из рабочей папки с документами. "
            "После удаления может потребоваться переиндексация (index_documents)."
        )

    @property
    def params_type(self) -> type[DeleteFileParams]:
        return DeleteFileParams

    def execute(self, params: DeleteFileParams) -> Iterator[DocToolOutput]:
        file_path = self._ws.source_file_path(params.filename)

        if not file_path.exists():
            yield ToolResult(content=f"Файл не найден: {params.filename}")
            return

        file_path.unlink()
        yield ToolResult(content=f"Файл удалён: {params.filename}")
