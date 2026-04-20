"""Tool: чтение содержимого файла (целиком или диапазон строк)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    Default,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    Ordered,
    ParamSchema,
    Required,
    Tool,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    UserWorkspaceService,
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class ReadFileArgs:
    filename: str
    encoding: str
    start_line: int | None = None
    end_line: int | None = None


class ReadFileArgsConverter(Converter[dict[str, Any], ReadFileArgs]):
    """Маппит провалидированный dict в :class:`ReadFileArgs`."""

    def convert(self, value: dict[str, Any]) -> ReadFileArgs:
        return ReadFileArgs(
            filename=value["filename"],
            encoding=value["encoding"],
            start_line=value.get("start_line"),
            end_line=value.get("end_line"),
        )


class ReadFileTool(Tool[ReadFileArgs]):
    """Чтение содержимого файла (целиком или диапазон строк 1-based)."""

    _ID = ToolId("read_file")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: UserWorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], ReadFileArgs]:
        return ReadFileArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Прочитать текстовое содержимое файла. По умолчанию "
                "возвращает файл целиком; через start_line/end_line можно "
                "запросить диапазон строк (1-based, включительно). Не "
                "подходит для бинарных файлов. Если файла нет — возвращает "
                "ошибку 'Файл не найден'."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="filename",
                        description="Путь к файлу.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="encoding",
                        description=(
                            "Текстовая кодировка файла, например 'utf-8' "
                            "или 'cp1251'. По умолчанию — 'utf-8'."
                        ),
                        validator=ChainValidator(
                            Default("utf-8"), IsString(), NonEmpty()
                        ),
                    ),
                    ParamSchema(
                        name="start_line",
                        description=(
                            "Начальная строка диапазона; 1 — первая строка. "
                            "Без end_line читается до конца файла. Опционально."
                        ),
                        validator=ChainValidator(IsInt(), MinValue(1)),
                    ),
                    ParamSchema(
                        name="end_line",
                        description=(
                            "Конечная строка диапазона, включительно. Без "
                            "start_line читается с первой строки. Должна "
                            "быть >= start_line. Опционально."
                        ),
                        validator=ChainValidator(IsInt(), MinValue(1)),
                    ),
                ],
                invariants=Ordered("start_line", "end_line"),
            ),
        )

    def execute(self, ctx: None, args: ReadFileArgs) -> ToolResult:
        try:
            with self._workspace.read_text(args.filename, args.encoding) as f:
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
