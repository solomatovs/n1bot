"""Tool: копирование файла или директории (cp / cp -r)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, IsBool, IsString, NonEmpty, Required
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

__all__ = ["CpTool", "CpToolConfig"]


@dataclass(frozen=True)
class CpArgs:
    src: str
    dst: str
    recursive: bool


@dataclass(frozen=True)
class CpToolConfig:
    prompt: PromptOverlay


class CpTool(Tool[CpArgs]):
    """Скопировать файл или директорию."""

    _NAME: ClassVar[ToolName] = ToolName("cp")

    def __init__(self, cfg: CpToolConfig, ctx: ExtensionContext, source_id: ToolSourceId) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[CpArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Скопировать файл или директорию. Для директорий "
                "требуется recursive=true."
            ),
            fields=[
                FieldSpec(
                    name="src",
                    description="Путь источника.",
                    coercer=ChainCoercer(Required(), IsString(), NonEmpty()),
                ),
                FieldSpec(
                    name="dst",
                    description="Путь назначения.",
                    coercer=ChainCoercer(Required(), IsString(), NonEmpty()),
                ),
                FieldSpec(
                    name="recursive",
                    description=(
                        "Рекурсивное копирование директории. По умолчанию false."
                    ),
                    coercer=ChainCoercer(Default(False), IsBool()),
                ),
            ],
            factory=CpArgs,
        ))

    def execute(self, ctx: ToolContext, req: CpArgs) -> ToolResult:
        try:
            ctx.project_workspace.copy(req.src, req.dst, recursive=req.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=f"Источник не найден: {req.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=f"Ошибка копирования: {e}",
            ) from e
        return TextResult(text=f"Скопировано: {req.src} → {req.dst}")
