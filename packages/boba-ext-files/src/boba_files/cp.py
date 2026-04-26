"""Tool: копирование файла или директории (cp / cp -r)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainConverter,
    Default,
    FieldSpec,
    IsBool,
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

    def definition(self) -> ObjectSchema[dict[str, Any]]:
        return ObjectSchema(
            description=(
                "Скопировать файл или директорию. Для директорий "
                "требуется recursive=true."
            ),
            fields=[
                    FieldSpec(
                        name="src",
                        description="Путь источника.",
                        converter=ChainConverter(Required(), IsString(), NonEmpty()),
                    ),
                    FieldSpec(
                        name="dst",
                        description="Путь назначения.",
                        converter=ChainConverter(Required(), IsString(), NonEmpty()),
                    ),
                    FieldSpec(
                        name="recursive",
                        description=(
                            "Рекурсивное копирование директории. "
                            "По умолчанию false."
                        ),
                        converter=ChainConverter(Default(False), IsBool()),
                    ),
                ],
                invariants=Pass()
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

