"""Tool: перезаписать файл целиком."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, IsString, NonEmpty, Required
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
    ToolSourceId,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError

__all__ = ["WriteTool", "WriteToolConfig"]


@dataclass(frozen=True)
class WriteArgs:
    path: str
    content: str
    encoding: str


@dataclass(frozen=True)
class WriteToolConfig:
    prompt: PromptOverlay


class WriteTool(Tool[WriteArgs]):
    """Полностью перезаписать файл содержимым."""

    _NAME: ClassVar[ToolName] = ToolName("write")

    def __init__(self, cfg: WriteToolConfig, ctx: ExtensionContext, source_id: ToolSourceId) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[WriteArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Перезаписать файл указанным содержимым. Если файла или "
                "промежуточных директорий нет — создать."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу.",
                    coercer=ChainCoercer(Required(), IsString(), NonEmpty()),
                ),
                FieldSpec(
                    name="content",
                    description="Новое содержимое файла.",
                    coercer=ChainCoercer(Required(), IsString()),
                ),
                FieldSpec(
                    name="encoding",
                    description="Кодировка файла. По умолчанию 'utf-8'.",
                    coercer=ChainCoercer(Default("utf-8"), IsString(), NonEmpty()),
                ),
            ],
            factory=WriteArgs,
        ))

    def execute(self, ctx: ToolContext, req: WriteArgs) -> ToolResult:
        existed = ctx.project_workspace.exists(req.path)
        try:
            with ctx.project_workspace.write_text(req.path, req.encoding) as f:
                f.write(req.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=f"Ошибка записи: {e}",
            ) from e
        action = "обновлён" if existed else "создан"
        return TextResult(
            text=f"Файл {action}: {req.path} ({len(req.content)} символов)",
        )
