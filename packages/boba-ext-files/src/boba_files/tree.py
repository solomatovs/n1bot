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
    Required,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
)


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

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], TreeArgs]:
        return TreeArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Рекурсивно перечислить все файлы под директорией. Плоский "
                "список путей. При переполнении limit ответ обрезается с "
                "маркером '(truncated at limit=N)'. Для одного уровня — ls."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Корень обхода. Без значения — корень workspace.",
                        validator=ChainValidator(IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="limit",
                        description="Максимум путей в ответе.",
                        validator=ChainValidator(Required(), IsInt(), MinValue(1)),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, req: TreeArgs) -> ToolResult:
        try:
            iterator = ctx.project_workspace.tree(req.path)
            items = list(islice(iterator, req.limit + 1))
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка обхода: {e}"
            ) from e

        truncated = len(items) > req.limit
        if truncated:
            items = items[: req.limit]

        location = req.path or "/"

        if not items:
            return ToolResult(content=f"{location} пуст.")

        header = f"Файлы {location} ({len(items)}, лимит={req.limit}"
        if truncated:
            header += f", truncated at limit={req.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"{header}\n{body}")

