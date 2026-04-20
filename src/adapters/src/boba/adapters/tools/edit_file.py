"""Tool: запись/обновление содержимого файла в workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.adapters.tools._workspace_arg import workspace_tool_id
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ParamSchema,
    Tool,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.validation import (
    ChainValidator,
    Default,
    IsString,
    NonEmpty,
    OneOf,
    Pass,
    Required,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceKind,
    WorkspaceResolver,
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

    _BASE_NAME = "edit_file"
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(
        self,
        resolver: WorkspaceResolver,
        workspace: WorkspaceKind,
    ) -> None:
        self._resolver = resolver
        self._workspace = workspace
        self._id = workspace_tool_id(workspace, self._BASE_NAME)

    def tool_id(self) -> ToolId:
        return self._id

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], EditFileArgs]:
        return EditFileArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                f"Записать в файл workspace '{self._workspace.name}'. "
                "В режиме 'write' перезаписывает целиком, в 'append' — "
                "дозаписывает в конец. Создаёт файл, если его не было."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="filename",
                        description="Путь к файлу внутри workspace.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="content",
                        description=(
                            "Содержимое для записи. В режиме 'write' — "
                            "полное новое содержимое; в 'append' — данные, "
                            "дописываемые в конец."
                        ),
                        validator=ChainValidator(Required(), IsString()),
                    ),
                    ParamSchema(
                        name="mode",
                        description=(
                            "Режим записи: 'write' — перезапись целиком, "
                            "'append' — дозапись в конец. По умолчанию — 'write'."
                        ),
                        validator=ChainValidator(
                            Default(WRITE_MODE),
                            IsString(),
                            OneOf(WRITE_MODE, APPEND_MODE),
                        ),
                    ),
                    ParamSchema(
                        name="encoding",
                        description="Кодировка файла. По умолчанию — utf-8.",
                        validator=ChainValidator(
                            Default("utf-8"), IsString(), NonEmpty()
                        ),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: EditFileArgs) -> ToolResult:
        workspace = self._resolver.resolve(self._workspace)
        existed = workspace.exists(args.filename)
        opener = (
            workspace.append_text if args.mode == APPEND_MODE else workspace.write_text
        )
        try:
            with opener(args.filename, args.encoding) as f:
                f.write(args.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._id, message=f"Ошибка записи: {e}"
            ) from e

        if args.mode == APPEND_MODE:
            action = "дозаписан" if existed else "создан (append)"
        else:
            action = "обновлён" if existed else "создан"
        return ToolResult(
            content=(
                f"Файл {action}: {self._workspace.name}:{args.filename} "
                f"({len(args.content)} символов)"
            )
        )
