"""Tool: рекурсивный обход workspace."""

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
    ProjectWorkspaceShell,
    WorkspaceError,
)


@dataclass(frozen=True)
class TreeArgs:
    path: str | None = None
    limit: int | None = None


class TreeArgsConverter(Converter[dict[str, Any], TreeArgs]):
    """Маппит провалидированный dict в :class:`TreeArgs`."""

    def convert(self, value: dict[str, Any]) -> TreeArgs:
        return TreeArgs(
            path=value.get("path"),
            limit=value.get("limit"),
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
                "Для одного уровня вложенности используй tool 'ls'."
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
                            "Максимум элементов в ответе (целое >= 0). "
                            "Без него возвращаются все."
                        ),
                        validator=ChainValidator(IsInt(), MinValue(0)),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: TreeArgs) -> ToolResult:
        try:
            iterator = self._workspace.tree(args.path)
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

        header = f"Файлы {location} ({len(items)}"
        if args.limit is not None:
            header += f", лимит={args.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"{header}\n{body}")
