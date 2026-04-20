"""Tool: запись/обновление содержимого файла в workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    Default,
    IsString,
    NonEmpty,
    OneOf,
    ParamSchema,
    Pass,
    Required,
    Tool,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    UserWorkspaceService,
    WorkspaceError,
)

WRITE_MODE = "write"
APPEND_MODE = "append"


@dataclass(frozen=True)
class EditFileArgs:
    filename: str
    content: str
    mode: str
    encoding: str


class EditFileArgsConverter(Converter[dict[str, Any], EditFileArgs]):
    """Маппит провалидированный dict в :class:`EditFileArgs`."""

    def convert(self, value: dict[str, Any]) -> EditFileArgs:
        return EditFileArgs(
            filename=value["filename"],
            content=value["content"],
            mode=value["mode"],
            encoding=value["encoding"],
        )


class EditFileTool(Tool[EditFileArgs]):
    """Запись/обновление файла в workspace."""

    _ID = ToolId("edit_file")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: UserWorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], EditFileArgs]:
        return EditFileArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Записать текст в файл. Режим 'write' (по умолчанию) "
                "полностью перезаписывает существующий файл; режим 'append' "
                "дозаписывает данные в конец. Если файла или промежуточных "
                "директорий нет — они создаются. Возвращает подтверждение с "
                "количеством записанных символов."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="filename",
                        description="Путь к файлу.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="content",
                        description=(
                            "Текст для записи. В режиме 'write' — полное "
                            "новое содержимое файла. В режиме 'append' — "
                            "данные, добавляемые в конец существующего "
                            "файла."
                        ),
                        validator=ChainValidator(Required(), IsString()),
                    ),
                    ParamSchema(
                        name="mode",
                        description=(
                            "Режим записи: 'write' — перезапись файла "
                            "целиком, 'append' — дозапись в конец. По "
                            "умолчанию — 'write'."
                        ),
                        validator=ChainValidator(
                            Default(WRITE_MODE),
                            IsString(),
                            OneOf(WRITE_MODE, APPEND_MODE),
                        ),
                    ),
                    ParamSchema(
                        name="encoding",
                        description=(
                            "Текстовая кодировка файла, например 'utf-8' "
                            "или 'cp1251'. По умолчанию — 'utf-8'."
                        ),
                        validator=ChainValidator(
                            Default("utf-8"), IsString(), NonEmpty()
                        ),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: EditFileArgs) -> ToolResult:
        existed = self._workspace.exists(args.filename)
        opener = (
            self._workspace.append_text
            if args.mode == APPEND_MODE
            else self._workspace.write_text
        )
        try:
            with opener(args.filename, args.encoding) as f:
                f.write(args.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка записи: {e}"
            ) from e

        if args.mode == APPEND_MODE:
            action = "дозаписан" if existed else "создан (append)"
        else:
            action = "обновлён" if existed else "создан"
        return ToolResult(
            content=f"Файл {action}: {args.filename} ({len(args.content)} символов)"
        )
