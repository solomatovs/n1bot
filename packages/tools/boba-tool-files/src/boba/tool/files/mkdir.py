"""Tool: создать директорию."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import ChainCoercer, IsString, NonEmpty, Required
from boba.schema.declaration import FieldSpec, ObjectSchema
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
from boba.workspace.contract import WorkspaceError

__all__ = ["MkdirTool", "MkdirToolConfig"]


@dataclass(frozen=True)
class MkdirArgs:
    path: str


@dataclass(frozen=True)
class MkdirToolConfig:
    prompt: PromptOverlay


class MkdirTool(Tool[MkdirArgs]):
    """Создать директорию."""

    _NAME: ClassVar[ToolName] = ToolName("mkdir")

    def __init__(self, cfg: MkdirToolConfig, ctx: ExtensionContext, source_id: ToolSourceId) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[MkdirArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Создать директорию (включая промежуточные). Если уже "
                "существует — no-op. Если по пути файл — ошибка."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь создаваемой директории.",
                    coercer=ChainCoercer(Required(), IsString(), NonEmpty()),
                ),
            ],
            factory=MkdirArgs,
        ))

    def execute(self, ctx: ToolContext, req: MkdirArgs) -> ToolResult:
        try:
            ctx.project_workspace.mkdir(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=f"Ошибка mkdir: {e}",
            ) from e
        return TextResult(text=f"Директория создана: {req.path}")
