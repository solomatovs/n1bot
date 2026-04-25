"""Tool: рекурсивный обход workspace."""

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
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSource,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    ProjectWorkspaceShell,
    WorkspaceError,
)
from boba.infra.plugins import PluginContext


@dataclass(frozen=True)
class TreeArgs:
    path: str | None
    limit: int


class TreeArgsConverter(Converter[dict[str, Any], TreeArgs]):
    """Маппит провалидированный dict в :class:`TreeArgs`."""

    def convert(self, value: dict[str, Any]) -> TreeArgs:
        return TreeArgs(
            path=value.get("path"),
            limit=value["limit"],
        )


class TreeTool(Tool[TreeArgs]):
    """Рекурсивный обход всех файлов workspace."""

    _ID = ToolId("tree")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: ProjectWorkspaceShell) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], TreeArgs]:
        return TreeArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Рекурсивно показать все файлы указанной директории, включая "
                "содержимое всех вложенных поддиректорий. Возвращает плоский "
                "список путей в порядке файловой системы, без сортировки. "
                "Для одного уровня вложенности используй tool 'ls'. Если "
                "файлов больше limit — ответ обрезается, в заголовке будет "
                "маркер '(truncated at limit=N)'."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description=(
                            "Корневая директория обхода. Без неё — обход "
                            "корневой директории."
                        ),
                        validator=ChainValidator(IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="limit",
                        description=(
                            "Максимум элементов в ответе (целое >= 1). "
                            "Обязательный параметр. Рекурсивный обход легко "
                            "выдаёт тысячи путей — подбирай значение "
                            "осознанно (разумные величины 100–1000); если "
                            "маркер усечения сработал, сузь scope через "
                            "более глубокий path."
                        ),
                        validator=ChainValidator(Required(), IsInt(), MinValue(1)),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: TreeArgs) -> ToolResult:
        try:
            iterator = self._workspace.tree(args.path)
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

        header = f"Файлы {location} ({len(items)}, лимит={args.limit}"
        if truncated:
            header += f", truncated at limit={args.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"{header}\n{body}")

def register(ctx: PluginContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files.tree"),
        priority=0,
        tools=[TreeTool(ctx.project_workspace)],
    )
