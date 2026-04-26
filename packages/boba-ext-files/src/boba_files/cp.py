"""Tool: копирование файла или директории (cp / cp -r)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


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

    def execute(self, ctx: ToolContext, req: CpArgs) -> ToolResult:
        try:
            ctx.project_workspace.copy(req.src, req.dst, recursive=req.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Источник не найден: {req.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка копирования: {e}",
            ) from e
        return ToolResult(content=f"Скопировано: {req.src} → {req.dst}")

