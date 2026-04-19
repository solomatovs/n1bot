"""Tool: чтение содержимого файла (целиком или диапазон строк)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    JsonType,
    ParamSchema,
    Tool,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
    WorkspaceService,
)


@dataclass(frozen=True)
class ReadFileArgs:
    filename: str
    start_line: int | None = None
    end_line: int | None = None


class ReadFileArgsConverter(Converter[dict[str, Any], ReadFileArgs]):
    def convert(self, value: dict[str, Any]) -> ReadFileArgs:
        sl = value.get("start_line")
        el = value.get("end_line")
        return ReadFileArgs(
            filename=str(value["filename"]),
            start_line=int(sl) if sl is not None else None,
            end_line=int(el) if el is not None else None,
        )


class ReadFileTool(Tool[ReadFileArgs]):
    """Чтение содержимого файла (целиком или диапазон строк 1-based)."""

    _ID = ToolId("read_file")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def args_converter(self) -> Converter[dict[str, Any], ReadFileArgs]:
        return ReadFileArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Прочитать содержимое файла из workspace. "
                "Опционально — диапазон строк (1-based, включительно)."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="filename",
                        type=JsonType.STRING,
                        description="Путь к файлу внутри workspace",
                    ),
                    ParamSchema(
                        name="start_line",
                        type=JsonType.INTEGER,
                        description="Начальная строка (с 1)",
                        required=False,
                    ),
                    ParamSchema(
                        name="end_line",
                        type=JsonType.INTEGER,
                        description="Конечная строка (включительно)",
                        required=False,
                    ),
                ]
            ),
        )

    def execute(self, ctx: None, args: ReadFileArgs) -> ToolResult:
        try:
            with self._workspace.read_text(args.filename) as f:
                text = f.read()
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Файл не найден: {args.filename}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка чтения: {e}"
            ) from e

        if args.start_line is not None or args.end_line is not None:
            lines = text.splitlines()
            start = max(1, args.start_line or 1)
            end = min(len(lines), args.end_line or len(lines))
            text = "\n".join(lines[start - 1 : end])
            label = f"{args.filename}:{start}-{end}"
        else:
            label = args.filename

        return ToolResult(content=f"### {label}\n\n{text}")
