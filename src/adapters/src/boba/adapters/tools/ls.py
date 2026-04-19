"""Tool: список элементов workspace без рекурсии."""

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
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import WorkspaceError, WorkspaceService


@dataclass(frozen=True)
class LsArgs:
    limit: int | None = None


class LsArgsConverter(Converter[dict[str, Any], LsArgs]):
    def convert(self, value: dict[str, Any]) -> LsArgs:
        limit = value.get("limit")
        return LsArgs(limit=int(limit) if limit is not None else None)


class LsTool(Tool[LsArgs]):
    """Плоский список элементов workspace (без рекурсии)."""

    _ID = ToolId("ls")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def args_converter(self) -> Converter[dict[str, Any], LsArgs]:
        return LsArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Показать элементы workspace на верхнем уровне (без рекурсии, "
                "без фильтрации). Порядок — как возвращает ФС, сортировка "
                "не применяется."
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

    def execute(self, ctx: None, args: LsArgs) -> ToolResult:
        if args.limit is not None and args.limit < 0:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"limit должен быть >= 0, получено {args.limit}",
            )

        try:
            iterator = self._workspace.ls()
            items = (
                list(iterator)
                if args.limit is None
                else list(islice(iterator, args.limit))
            )
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка обхода workspace: {e}"
            ) from e

        if not items:
            return ToolResult(content="Workspace пуст.")

        header = f"Элементы ({len(items)}"
        if args.limit is not None:
            header += f", лимит={args.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"{header}\n{body}")
