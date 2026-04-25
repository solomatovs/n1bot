"""Tool: копирование файла или директории (cp / cp -r)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from boba.adapters.tool_providers import StaticToolSource
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    Default,
    IsBool,
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
    WorkspaceNotFoundError,
)
from boba.infra.extensions import ExtensionContext


@dataclass(frozen=True)
class CpArgs:
    src: str
    dst: str
    recursive: bool


class CpArgsConverter(Converter[dict[str, Any], CpArgs]):
    def convert(self, value: dict[str, Any]) -> CpArgs:
        return CpArgs(
            src=value["src"],
            dst=value["dst"],
            recursive=value["recursive"],
        )


class CpTool(Tool[CpArgs]):
    """Скопировать файл или директорию."""

    _ID = ToolId("cp")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], CpArgs]:
        return CpArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Скопировать файл или директорию. Для директорий "
                "требуется recursive=true."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="src",
                        description="Путь источника.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="dst",
                        description="Путь назначения.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="recursive",
                        description=(
                            "Рекурсивное копирование директории. "
                            "По умолчанию false."
                        ),
                        validator=ChainValidator(Default(False), IsBool()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, args: CpArgs) -> ToolResult:
        try:
            ctx.project_workspace.copy(args.src, args.dst, recursive=args.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Источник не найден: {args.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка копирования: {e}",
            ) from e
        return ToolResult(content=f"Скопировано: {args.src} → {args.dst}")

def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files.cp"),
        priority=0,
        tools=[CpTool()],
    )
