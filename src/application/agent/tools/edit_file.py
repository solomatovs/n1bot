"""Tool: редактирование/создание файла в рабочей папке."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator

from domain.agent.events import DocPipelineEvent
from domain.agent.tools import Tool, ToolOutput, ToolResult
from domain.workspace import Workspace

DocToolOutput = ToolOutput[DocPipelineEvent]


@dataclass(frozen=True)
class EditFileParams:
    """Параметры записи файла."""
    filename: str = field(metadata={"description": "Имя файла"})
    content: str = field(metadata={"description": "Новое содержимое файла"})


class EditFileTool(Tool[DocPipelineEvent, EditFileParams]):
    """Запись/обновление содержимого файла в рабочей папке."""

    def __init__(self, ws: Workspace) -> None:
        self._ws = ws

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Записать или обновить содержимое файла в рабочей папке. "
            "Создаёт файл если не существует. Перезаписывает если существует. "
            "После изменений может потребоваться переиндексация (index_documents)."
        )

    @property
    def params_type(self) -> type[EditFileParams]:
        return EditFileParams

    def execute(self, params: EditFileParams) -> Iterator[DocToolOutput]:
        file_path = self._ws.source_file_path(params.filename)
        existed = file_path.exists()
        file_path.write_text(params.content, encoding="utf-8")

        action = "обновлён" if existed else "создан"
        yield ToolResult(content=f"Файл {action}: {params.filename} ({len(params.content)} символов)")
