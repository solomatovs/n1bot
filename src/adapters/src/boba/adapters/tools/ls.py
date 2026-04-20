"""Tool: список элементов workspace без рекурсии."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    ParamSchema,
    Pass,
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


@dataclass(frozen=True)
class LsArgs:
    path: str | None = None
    limit: int | None = None


class LsArgsConverter(Converter[dict[str, Any], LsArgs]):
    """Маппит провалидированный dict в :class:`LsArgs`.

    Все проверки (тип, длина, min) уже сделаны
    :class:`SchemaArgsValidator` — здесь только сборка dataclass.
    """

    def convert(self, value: dict[str, Any]) -> LsArgs:
        return LsArgs(
            path=value.get("path"),
            limit=value.get("limit"),
        )


class LsTool(Tool[LsArgs]):
    """Плоский список элементов workspace (без рекурсии)."""

    _ID = ToolId("ls")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: UserWorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], LsArgs]:
        return LsArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Показать содержимое указанной директории на одном уровне "
                "(без рекурсии). Возвращает имена файлов и поддиректорий в "
                "порядке файловой системы, без сортировки. Для рекурсивного "
                "обхода используй tool 'tree'."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description=(
                            "Путь директории. Без него листится корневая "
                            "директория."
                        ),
                        validator=ChainValidator(IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="limit",
                        description=(
                            "Максимум элементов в ответе (целое >= 0). "
                            "Без него возвращаются все."
                        ),
                        validator=ChainValidator(IsInt(), MinValue(0)),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: LsArgs) -> ToolResult:
        try:
            iterator = self._workspace.ls(args.path)
            items = (
                list(iterator)
                if args.limit is None
                else list(islice(iterator, args.limit))
            )
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка обхода: {e}"
            ) from e

        location = args.path or "/"

        if not items:
            return ToolResult(content=f"{location} пуст.")

        header = f"Элементы {location} ({len(items)}"
        if args.limit is not None:
            header += f", лимит={args.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"{header}\n{body}")
