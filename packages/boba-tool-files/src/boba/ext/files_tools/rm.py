"""Tool: удаление файла или директории (rm / rm -r)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, IsBool, IsString, NonEmpty
from boba.declaration import FieldSpec, ObjectSchema
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import (
    TextResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba.workspace import WorkspaceError, WorkspaceNotFoundError

__all__ = ["RmTool", "RmToolConfig"]


@dataclass(frozen=True)
class RmArgs:
    path: str
    recursive: bool


@dataclass(frozen=True)
class RmToolConfig:
    prompt: PromptOverlay


class RmTool(Tool[RmArgs]):
    """Удалить файл или директорию."""

    _ID: ClassVar[ToolId] = ToolId("rm")
    _SOURCE: ClassVar[ToolSourceId] = ToolSourceId("plugin.files")

    def __init__(self, cfg: RmToolConfig, ctx: ExtensionContext) -> None:
        self._cfg = cfg
        self._ctx = ctx

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[RmArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Удалить файл или директорию. Для директорий требуется "
                "recursive=true. Безвозвратно."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу или директории.",
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="recursive",
                    description=(
                        "Удалить директорию со всем содержимым. По умолчанию false."
                    ),
                    coercer=ChainCoercer(Default(False), IsBool()),
                ),
            ],
            factory=RmArgs,
        ))

    def execute(self, ctx: ToolContext, req: RmArgs) -> ToolResult:
        try:
            ctx.project_workspace.delete(req.path, recursive=req.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Не найдено: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка удаления: {e}",
            ) from e
        return TextResult(text=f"Удалено: {req.path}")
