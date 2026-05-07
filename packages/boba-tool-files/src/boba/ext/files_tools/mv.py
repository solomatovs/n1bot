"""Tool: переместить/переименовать файл или директорию."""

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
from boba.workspace import WorkspaceError, WorkspaceNotFoundError

__all__ = ["MvTool", "MvToolConfig"]


@dataclass(frozen=True)
class MvArgs:
    src: str
    dst: str


@dataclass(frozen=True)
class MvToolConfig:
    prompt: PromptOverlay


class MvTool(Tool[MvArgs]):
    """Переместить/переименовать файл или директорию."""

    _ID: ClassVar[ToolId] = ToolId("mv")
    _SOURCE: ClassVar[ToolSourceId] = ToolSourceId("plugin.files")

    def __init__(self, cfg: MvToolConfig, ctx: ExtensionContext) -> None:
        self._cfg = cfg
        self._ctx = ctx

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[MvArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Переместить или переименовать файл/директорию. Если dst — "
                "существующая директория, src переносится внутрь. Файл по "
                "пути dst перезаписывается. Промежуточные директории не "
                "создаются."
            ),
            fields=[
                FieldSpec(
                    name="src",
                    description="Путь источника.",
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="dst",
                    description="Путь назначения.",
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
            ],
            factory=MvArgs,
        ))

    def execute(self, ctx: ToolContext, req: MvArgs) -> ToolResult:
        try:
            ctx.project_workspace.move(req.src, req.dst)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Источник не найден: {req.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка перемещения: {e}",
            ) from e
        return TextResult(text=f"Перемещено: {req.src} → {req.dst}")
