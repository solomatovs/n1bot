"""Tool: find-and-replace редактирование файла."""

from __future__ import annotations

from dataclasses import dataclass

from boba_next.declaration import FieldSpec, ObjectSchema
from boba_next.tools import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba_next.validators import (
    ChainConverter,
    Default,
    IsBool,
    IsString,
    NonEmpty,
)
from boba_next.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class EditArgs:
    path: str
    old_string: str
    new_string: str
    replace_all: bool
    encoding: str


class EditTool(Tool[EditArgs]):
    """Find-and-replace редактирование текстового файла."""

    _ID = ToolId("edit")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[EditArgs]:
        return ObjectSchema(
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
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="old_string",
                    description="Подстрока для замены. Совпадение точное.",
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="new_string",
                    description="Заменяющий текст. Пустая строка = удаление.",
                    converter=ChainConverter(IsString()),
                    required=True,
                ),
                FieldSpec(
                    name="replace_all",
                    description="Заменить все вхождения. По умолчанию false.",
                    converter=ChainConverter(Default(False), IsBool()),
                ),
                FieldSpec(
                    name="encoding",
                    description="Кодировка файла. По умолчанию 'utf-8'.",
                    converter=ChainConverter(
                        Default("utf-8"),
                        IsString(),
                        NonEmpty(),
                    ),
                ),
            ],
            factory=EditArgs,
        )

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
        return ToolResult(
            content=f"Заменено в {req.path}: {applied} вхождение(й).",
        )
