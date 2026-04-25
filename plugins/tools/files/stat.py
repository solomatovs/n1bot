"""Tool: метаданные файла или директории."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from boba.adapters.tool_providers import StaticToolSource
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
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
from boba.infra.plugins import PluginContext


@dataclass(frozen=True)
class StatArgs:
    path: str


class StatArgsConverter(Converter[dict[str, Any], StatArgs]):
    def convert(self, value: dict[str, Any]) -> StatArgs:
        return StatArgs(path=value["path"])


class StatTool(Tool[StatArgs]):
    """Метаданные файла или директории."""

    _ID = ToolId("stat")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], StatArgs]:
        return StatArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Показать метаданные ресурса: тип (file/directory/other), "
                "размер в байтах и время модификации. Если ресурса нет — "
                "возвращает ошибку.\n"
                "\n"
                "Для директорий поле size — это размер inode-блока "
                "файловой системы (обычно 4096), а НЕ количество файлов "
                "и НЕ признак пустоты. Узнать, что лежит внутри "
                "директории, через stat нельзя — для этого используй "
                "инструмент ls (один уровень) или tree (рекурсивно)."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Путь к файлу или директории.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, args: StatArgs) -> ToolResult:
        try:
            meta = ctx.project_workspace.meta(args.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Не найдено: {args.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка stat: {e}",
            ) from e

        body = (
            f"path: {meta.path}\n"
            f"kind: {meta.kind}\n"
            f"size: {meta.size}\n"
            f"modified: {meta.modified.isoformat()}"
        )
        return ToolResult(content=body)

def register(ctx: PluginContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files.stat"),
        priority=0,
        tools=[StatTool()],
    )
