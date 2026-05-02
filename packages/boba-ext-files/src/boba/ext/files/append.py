"""Tool: дозаписать в конец файла."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.tools import (
    ChainConverter,
    Default,
    FieldSpec,
    IsString,
    NonEmpty,
    ObjectSchema,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
)
from boba.patterns import Converter


@dataclass(frozen=True)
class AppendArgs:
    path: str
    content: str
    encoding: str


class AppendArgsConverter(Converter[dict[str, Any], AppendArgs]):
    def convert(self, value: dict[str, Any]) -> AppendArgs:
        return AppendArgs(
            path=value["path"],
            content=value["content"],
            encoding=value["encoding"],
        )


class AppendTool(Tool[AppendArgs]):
    """Дозаписать текст в конец файла."""

    _ID = ToolId("append")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], AppendArgs]:
        return AppendArgsConverter()

    def definition(self) -> ObjectSchema[dict[str, Any]]:
        return ObjectSchema(
            description="Дописать текст в конец файла. Если файла нет — создать.",
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу.",
                    converter=ChainConverter(Required(), IsString(), NonEmpty()),
                ),
                FieldSpec(
                    name="content",
                    description="Дописываемый текст.",
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
        )

    def execute(self, ctx: ToolContext, req: AppendArgs) -> ToolResult:
        existed = ctx.project_workspace.exists(req.path)
        try:
            with ctx.project_workspace.append_text(req.path, req.encoding) as f:
                f.write(req.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка записи: {e}",
            ) from e
        action = "дозаписан" if existed else "создан"
        return ToolResult(
            content=f"Файл {action}: {req.path} ({len(req.content)} символов)",
        )
