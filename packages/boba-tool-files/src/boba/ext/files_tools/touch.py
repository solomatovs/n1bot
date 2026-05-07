"""Tool: создать пустой файл или обновить mtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, IsString, NonEmpty
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
from boba.workspace import WorkspaceError

__all__ = ["TouchTool", "TouchToolConfig"]


@dataclass(frozen=True)
class TouchArgs:
    path: str


@dataclass(frozen=True)
class TouchToolConfig:
    prompt: PromptOverlay


class TouchTool(Tool[TouchArgs]):
    """Создать пустой файл или обновить mtime существующего."""

    _ID: ClassVar[ToolId] = ToolId("touch")
    _SOURCE: ClassVar[ToolSourceId] = ToolSourceId("plugin.files")

    def __init__(self, cfg: TouchToolConfig, ctx: ExtensionContext) -> None:
        self._cfg = cfg
        self._ctx = ctx

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[TouchArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Создать пустой файл (включая промежуточные директории). "
                "Если уже существует — обновить время модификации, "
                "содержимое не трогать."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу.",
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
            ],
            factory=TouchArgs,
        ))

    def execute(self, ctx: ToolContext, req: TouchArgs) -> ToolResult:
        try:
            ctx.project_workspace.touch(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка touch: {e}",
            ) from e
        return TextResult(text=f"touch: {req.path}")
