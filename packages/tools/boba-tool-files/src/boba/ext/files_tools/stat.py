"""Tool: метаданные файла или директории."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, IsString, NonEmpty, Required
from boba.declaration import FieldSpec, ObjectSchema
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolName,
    ToolResult,
    ToolSourceId,
)
from boba.workspace.contract import WorkspaceError, WorkspaceNotFoundError

__all__ = ["StatTool", "StatToolConfig"]


@dataclass(frozen=True)
class StatArgs:
    path: str


@dataclass(frozen=True)
class StatToolConfig:
    prompt: PromptOverlay


class StatTool(Tool[StatArgs]):
    """Метаданные файла или директории."""

    _NAME: ClassVar[ToolName] = ToolName("stat")

    def __init__(self, cfg: StatToolConfig, ctx: ExtensionContext, source_id: ToolSourceId) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[StatArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Вернуть метаданные ресурса: тип (file/directory/other), "
                "размер в байтах, время модификации. Если ресурса нет — "
                "ошибка. Для директорий size — размер inode-блока ФС, не "
                "количество файлов; для содержимого директории — ls/tree."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу или директории.",
                    coercer=ChainCoercer(Required(), IsString(), NonEmpty()),
                ),
            ],
            factory=StatArgs,
        ))

    def execute(self, ctx: ToolContext, req: StatArgs) -> ToolResult:
        try:
            meta = ctx.project_workspace.meta(req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=f"Не найдено: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=f"Ошибка stat: {e}",
            ) from e

        body = {
            "path": meta.path,
            "kind": meta.kind,
            "size": meta.size,
            "modified": meta.modified.isoformat()
        }

        return JsonResult(body)
