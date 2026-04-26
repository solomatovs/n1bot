"""Tool: дозаписать в конец файла."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from boba.adapters.tool_providers import StaticToolSource
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    Default,
    IsString,
    NonEmpty,
    ParamSchema,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSource,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
)
from boba.infra.extensions import ExtensionContext


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

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description="Дописать текст в конец файла. Если файла нет — создать.",
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Путь к файлу.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="content",
                        description="Дописываемый текст.",
                        validator=ChainValidator(Required(), IsString()),
                    ),
                    ParamSchema(
                        name="encoding",
                        description="Кодировка файла. По умолчанию 'utf-8'.",
                        validator=ChainValidator(
                            Default("utf-8"),
                            IsString(),
                            NonEmpty(),
                        ),
                    ),
                ],
                invariants=Pass(),
            ),
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


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files.append"),
        priority=0,
        tools=[AppendTool()],
    )
