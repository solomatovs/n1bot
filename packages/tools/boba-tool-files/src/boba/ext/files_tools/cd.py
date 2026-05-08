"""Tool: сменить текущую директорию."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, IsString, NonEmpty, Required
from boba.declaration import FieldSpec, ObjectSchema
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import (
    TextResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolName,
    ToolResult,
    ToolSourceId,
)
from boba.workspace.contract import WorkspaceError, WorkspaceNotFoundError

__all__ = ["CdTool", "CdToolConfig"]


@dataclass(frozen=True)
class CdArgs:
    path: str


@dataclass(frozen=True)
class CdToolConfig:
    prompt: PromptOverlay


class CdTool(Tool[CdArgs]):
    """Сменить текущую директорию."""

    _NAME: ClassVar[ToolName] = ToolName("cd")

    def __init__(self, cfg: CdToolConfig, ctx: ExtensionContext, source_id: ToolSourceId) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[CdArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description="Сменить текущую директорию.",
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь директории.",
                    coercer=ChainCoercer(Required(), IsString(), NonEmpty()),
                ),
            ],
            factory=CdArgs,
        ))

    def execute(self, ctx: ToolContext, req: CdArgs) -> ToolResult:
        try:
            ctx.project_workspace.cd(req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=f"Директория не найдена: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=f"Ошибка cd: {e}",
            ) from e
        return TextResult(text=f"Текущая директория: {ctx.project_workspace.cwd}")
