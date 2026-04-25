"""Tool: список элементов workspace без рекурсии."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from typing import Any

from boba.adapters.tool_providers import StaticToolSource
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    IsInt,
    IsString,
    MinValue,
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
)
from boba.infra.extensions import ExtensionContext


@dataclass(frozen=True)
class LsArgs:
    path: str | None
    limit: int


class LsArgsConverter(Converter[dict[str, Any], LsArgs]):
    """Маппит провалидированный dict в :class:`LsArgs`.

    Все проверки (тип, длина, min) уже сделаны
    :class:`SchemaArgsValidator` — здесь только сборка dataclass.
    """

    def convert(self, value: dict[str, Any]) -> LsArgs:
        return LsArgs(
            path=value.get("path"),
            limit=value["limit"],
        )


class LsTool(Tool[LsArgs]):
    """Плоский список элементов workspace (без рекурсии)."""

    _ID = ToolId("ls")
    _SOURCE = ToolSourceId("builtin.files")

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
                "порядке файловой системы, без сортировки. limit обязательный аргумент!"
                "Для рекурсивного обхода используй tool 'tree'. "
                "Если элементов больше limit "
                "— ответ обрезается, в заголовке будет маркер "
                "'(truncated at limit=N)'."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description=(
                            "Путь директории. Без него листится корневая директория."
                        ),
                        validator=ChainValidator(IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="limit",
                        description=(
                            "Максимум элементов в ответе (целое >= 1). "
                            "Обязательный параметр. Подбирай осознанно: "
                            "большие значения раздувают контекст. "
                            "Разумные величины — 50–500."
                        ),
                        validator=ChainValidator(Required(), IsInt(), MinValue(1)),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, args: LsArgs) -> ToolResult:
        try:
            iterator = ctx.project_workspace.ls(args.path)
            items = list(islice(iterator, args.limit + 1))
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка обхода: {e}"
            ) from e

        truncated = len(items) > args.limit
        if truncated:
            items = items[: args.limit]

        location = args.path or "/"

        if not items:
            return ToolResult(content=f"{location} пуст.")

        header = f"Элементы {location} ({len(items)}, лимит={args.limit}"
        if truncated:
            header += f", truncated at limit={args.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"{header}\n{body}")

def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files.ls"),
        priority=0,
        tools=[LsTool()],
    )
