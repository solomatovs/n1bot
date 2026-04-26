"""Tool: перезаписать файл целиком."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainConverter,
    Default,
    IsString,
    NonEmpty,
    FieldSpec,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
)


@dataclass(frozen=True)
class WriteArgs:
    path: str
    content: str
    encoding: str


class WriteArgsConverter(Converter[dict[str, Any], WriteArgs]):
    def convert(self, value: dict[str, Any]) -> WriteArgs:
        return WriteArgs(
            path=value["path"],
            content=value["content"],
            encoding=value["encoding"],
        )


class WriteTool(Tool[WriteArgs]):
    """Полностью перезаписать файл содержимым."""

    _ID = ToolId("write")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], WriteArgs]:
        return WriteArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Перезаписать файл указанным содержимым. Если файла или "
                "промежуточных директорий нет — создать."
            ),
            input_schema=ToolInputSchema(
                params=[
                    FieldSpec(
                        name="path",
                        description="Путь к файлу.",
                        converter=ChainConverter(Required(), IsString(), NonEmpty()),
                    ),
                    FieldSpec(
                        name="content",
                        description="Новое содержимое файла.",
                        converter=ChainConverter(Required(), IsString()),
                    ),
                    FieldSpec(
                        name="encoding",
                        description="Кодировка файла. По умолчанию 'utf-8'.",
                        converter=ChainConverter(
                            Default("utf-8"),
                            IsString(),
                            NonEmpty(),
                        ),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, req: WriteArgs) -> ToolResult:
        existed = ctx.project_workspace.exists(req.path)
        try:
            with ctx.project_workspace.write_text(req.path, req.encoding) as f:
                f.write(req.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка записи: {e}",
            ) from e
        action = "обновлён" if existed else "создан"
        return ToolResult(
            content=f"Файл {action}: {req.path} ({len(req.content)} символов)",
        )

