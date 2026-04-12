"""Tool: чтение содержимого документа."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from boba_domain.workspace import Workspace
from boba_domain.agent.events import ContextReady, DocPipelineEvent
from boba_domain.search.types import ChunkLocation, Fragment, SearchHit
from boba_domain.core.tools import Tool, ToolEvent, ToolOutput, ToolResult

DocToolOutput = ToolOutput[DocPipelineEvent]


@dataclass(frozen=True)
class ReadFileParams:
    """Параметры чтения файла."""

    filename: str = field(
        metadata={
            "description": "Имя файла (из результатов search_documents или list_files)"
        }
    )
    start_line: int | None = field(
        default=None, metadata={"description": "Начальная строка (нумерация с 1)"}
    )
    end_line: int | None = field(
        default=None, metadata={"description": "Конечная строка"}
    )


class ReadFileTool(Tool[DocPipelineEvent, ReadFileParams]):
    """Чтение содержимого документа (целиком или диапазон строк)."""

    MAX_RESULT_CHARS = 4000

    def __init__(self, ws: Workspace) -> None:
        self._ws = ws

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Прочитать содержимое документа. "
            "Можно указать диапазон строк для чтения части файла. "
            "Используй после search_documents для получения полного контекста."
        )

    @property
    def params_type(self) -> type[ReadFileParams]:
        return ReadFileParams

    @staticmethod
    def read_line_range(file_path, start_line: int, end_line: int) -> str:
        """Прочитать диапазон строк из файла (1-based)."""
        lines: list[str] = []
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if i > end_line:
                    break
                if i >= start_line:
                    lines.append(line.rstrip("\n"))
        return "\n".join(lines)

    def execute(self, params: ReadFileParams) -> Iterator[DocToolOutput]:
        file_path = self._ws.source_file_path(params.filename)

        if not file_path.exists():
            yield ToolResult(content=f"Файл не найден: {params.filename}")
            return

        if params.start_line is not None and params.end_line is not None:
            text = self.read_line_range(file_path, params.start_line, params.end_line)
            label = f"{params.filename}:{params.start_line}-{params.end_line}"
        else:
            text = file_path.read_text(encoding="utf-8", errors="replace")
            label = params.filename

        text = text[: self.MAX_RESULT_CHARS]

        fragment = Fragment(
            text=text,
            hit=SearchHit(
                content="",
                location=ChunkLocation(
                    source_file=params.filename,
                    start_line=params.start_line or 1,
                    end_line=params.end_line or text.count("\n") + 1,
                    start_offset=0,
                    end_offset=len(text.encode("utf-8")),
                ),
            ),
            read_start_line=params.start_line or 1,
            read_end_line=params.end_line or text.count("\n") + 1,
            read_start_offset=0,
            read_end_offset=len(text.encode("utf-8")),
        )
        yield ToolEvent(ContextReady(context=text, fragments=[fragment]))
        yield ToolResult(content=f"### {label}\n\n{text}")
