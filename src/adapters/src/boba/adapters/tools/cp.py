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

    def __init__(self, workspace: UserWorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], CpArgs]:
        return CpArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Скопировать файл или директорию. Файл копируется всегда. "
                "Директория — только с recursive=true (рекурсивно со всем "
                "содержимым). Если dst — существующая директория, копия "
                "кладётся внутрь с именем src. Существующий файл по dst "
                "перезаписывается."
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
                            "Если true — рекурсивно скопировать директорию. "
                            "По умолчанию false."
                        ),
                        validator=ChainValidator(Default(False), IsBool()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: CpArgs) -> ToolResult:
        try:
            self._workspace.copy(args.src, args.dst, recursive=args.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Источник не найден: {args.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка копирования: {e}",
            ) from e
        return ToolResult(content=f"Скопировано: {args.src} → {args.dst}")
