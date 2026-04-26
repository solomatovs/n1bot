"""Tool: метаданные файла или директории."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


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
                "Вернуть метаданные ресурса: тип (file/directory/other), "
                "размер в байтах, время модификации. Если ресурса нет — "
                "ошибка. Для директорий size — размер inode-блока ФС, не "
                "количество файлов; для содержимого директории — ls/tree."
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

    def execute(self, ctx: ToolContext, req: StatArgs) -> ToolResult:
        try:
            meta = ctx.project_workspace.meta(req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Не найдено: {req.path}",
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

