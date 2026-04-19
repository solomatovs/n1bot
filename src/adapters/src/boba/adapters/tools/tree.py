"""Tool: рекурсивный обход workspace."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    JsonType,
    ParamSchema,
    Tool,
    ToolDefinition,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import WorkspaceError, WorkspaceService


@dataclass(frozen=True)
class TreeArgs:
    limit: int | None = None


class TreeArgsConverter(Converter[dict[str, Any], TreeArgs]):
    def convert(self, value: dict[str, Any]) -> TreeArgs:
        limit = value.get("limit")
        return TreeArgs(limit=int(limit) if limit is not None else None)


class TreeTool(Tool[TreeArgs]):
    """Рекурсивный обход всех файлов workspace."""

    _ID = ToolId("tree")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def args_converter(self) -> Converter[dict[str, Any], TreeArgs]:
        return TreeArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Показать все файлы workspace рекурсивно (без фильтрации). "
                "Порядок — как возвращает ФС, сортировка не применяется."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="limit",
                        type=JsonType.INTEGER,
                        description=(
                            "Опциональный лимит количества элементов "
                            "в ответе. Без него возвращается всё."
                        ),
                        required=False,
                    ),
                ]
            ),
        )

    def execute(self, ctx: None, args: TreeArgs) -> ToolResult:
        if args.limit is not None and args.limit < 0:
            return ToolResult(
                content=f"limit должен быть >= 0, получено {args.limit}",
                is_error=True,
            )

        try:
            iterator = self._workspace.tree()
            items = (
                list(iterator)
                if args.limit is None
                else list(islice(iterator, args.limit))
            )
        except WorkspaceError as e:
            return ToolResult(content=f"Ошибка обхода workspace: {e}", is_error=True)

        if not items:
            return ToolResult(content="Workspace пуст.")

        header = f"Файлы ({len(items)}"
        if args.limit is not None:
            header += f", лимит={args.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"{header}\n{body}")
