"""Tool: find-and-replace редактирование файла."""

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

__all__ = ["EditTool", "EditToolConfig"]


@dataclass(frozen=True)
class EditArgs:
    path: str
    old_string: str
    new_string: str
    replace_all: bool
    encoding: str


@dataclass(frozen=True)
class EditToolConfig:
    prompt: PromptOverlay


class EditTool(Tool[EditArgs]):
    """Find-and-replace редактирование текстового файла."""

    _ID: ClassVar[ToolId] = ToolId("edit")
    _SOURCE: ClassVar[ToolSourceId] = ToolSourceId("plugin.files")

    def __init__(self, cfg: EditToolConfig, ctx: ExtensionContext) -> None:
        self._cfg = cfg
        self._ctx = ctx

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[EditArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Заменить подстроку old_string на new_string. По умолчанию "
                "old_string должна встречаться в файле ровно один раз — "
                "иначе ошибка. С replace_all=true заменяются все вхождения. "
                "Совпадение точное, посимвольное."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу.",
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="old_string",
                    description="Подстрока для замены. Совпадение точное.",
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="new_string",
                    description="Заменяющий текст. Пустая строка = удаление.",
                    coercer=ChainCoercer(IsString()),
                    required=True,
                ),
                FieldSpec(
                    name="replace_all",
                    description="Заменить все вхождения. По умолчанию false.",
                    coercer=ChainCoercer(Default(False), IsBool()),
                ),
                FieldSpec(
                    name="encoding",
                    description="Кодировка файла. По умолчанию 'utf-8'.",
                    coercer=ChainCoercer(Default("utf-8"), IsString(), NonEmpty()),
                ),
            ],
            factory=EditArgs,
        ))

    def execute(self, ctx: ToolContext, req: EditArgs) -> ToolResult:
        try:
            applied = ctx.project_workspace.edit_text(
                req.path,
                req.old_string,
                req.new_string,
                replace_all=req.replace_all,
                encoding=req.encoding,
            )
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Файл не найден: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка edit: {e}",
            ) from e
        return TextResult(
            text=f"Заменено в {req.path}: {applied} вхождение(й).",
        )
