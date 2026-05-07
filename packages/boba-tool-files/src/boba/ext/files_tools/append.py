"""Tool: дозаписать в конец файла."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, IsString, NonEmpty
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
from boba.workspace import WorkspaceError

__all__ = ["AppendTool", "AppendToolConfig"]


@dataclass(frozen=True)
class AppendArgs:
    path: str
    content: str
    encoding: str


@dataclass(frozen=True)
class AppendToolConfig:
    prompt: PromptOverlay


class AppendTool(Tool[AppendArgs]):
    """Дозаписать текст в конец файла."""

    _NAME: ClassVar[ToolName] = ToolName("append")

    def __init__(self, cfg: AppendToolConfig, ctx: ExtensionContext, source_id: ToolSourceId) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[AppendArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description="Дописать текст в конец файла. Если файла нет — создать.",
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу.",
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="content",
                    description="Дописываемый текст.",
                    coercer=ChainCoercer(IsString()),
                    required=True,
                ),
                FieldSpec(
                    name="encoding",
                    description="Кодировка файла. По умолчанию 'utf-8'.",
                    coercer=ChainCoercer(Default("utf-8"), IsString(), NonEmpty()),
                ),
            ],
            factory=AppendArgs,
        ))

    def execute(self, ctx: ToolContext, req: AppendArgs) -> ToolResult:
        existed = ctx.project_workspace.exists(req.path)
        try:
            with ctx.project_workspace.append_text(req.path, req.encoding) as f:
                f.write(req.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=f"Ошибка записи: {e}",
            ) from e
        action = "дозаписан" if existed else "создан"
        return TextResult(
            text=f"Файл {action}: {req.path} ({len(req.content)} символов)",
        )
