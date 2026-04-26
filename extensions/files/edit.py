"""Tool: find-and-replace редактирование файла."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from boba.adapters.tool_providers import StaticToolSource
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    Default,
    IsBool,
    IsString,
    NonEmpty,
    ParamSchema,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSource,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)
from boba.infra.extensions import ExtensionContext


@dataclass(frozen=True)
class EditArgs:
    path: str
    old_string: str
    new_string: str
    replace_all: bool
    encoding: str


class EditArgsConverter(Converter[dict[str, Any], EditArgs]):
    def convert(self, value: dict[str, Any]) -> EditArgs:
        return EditArgs(
            path=value["path"],
            old_string=value["old_string"],
            new_string=value["new_string"],
            replace_all=value["replace_all"],
            encoding=value["encoding"],
        )


class EditTool(Tool[EditArgs]):
    """Find-and-replace редактирование текстового файла."""

    _ID = ToolId("edit")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], EditArgs]:
        return EditArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Заменить подстроку old_string на new_string. По умолчанию "
                "old_string должна встречаться в файле ровно один раз — "
                "иначе ошибка. С replace_all=true заменяются все вхождения. "
                "Совпадение точное, посимвольное."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Путь к файлу.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="old_string",
                        description="Подстрока для замены. Совпадение точное.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="new_string",
                        description="Заменяющий текст. Пустая строка = удаление.",
                        validator=ChainValidator(Required(), IsString()),
                    ),
                    ParamSchema(
                        name="replace_all",
                        description="Заменить все вхождения. По умолчанию false.",
                        validator=ChainValidator(Default(False), IsBool()),
                    ),
                    ParamSchema(
                        name="encoding",
                        description="Кодировка файла. По умолчанию 'utf-8'.",
                        validator=ChainValidator(
                            Default("utf-8"),
                            IsString(),
                            NonEmpty(),
                        ),
                    ),
                ],
                invariants=Pass(),
            ),
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

def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files.edit"),
        priority=0,
        tools=[EditTool()],
    )
