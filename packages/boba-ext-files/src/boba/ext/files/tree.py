"""Tool: рекурсивный обход workspace."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice

from boba_next.declaration import FieldSpec, ObjectSchema
from boba_next.tools import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba_next.validators import (
    ChainConverter,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    Nullable,
)
from boba_next.workspace import (
    WorkspaceError,
)


@dataclass(frozen=True)
class TreeArgs:
    path: str | None
    limit: int


class TreeTool(Tool[TreeArgs]):
    """Рекурсивный обход всех файлов workspace."""

    _ID = ToolId("tree")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[TreeArgs]:
        return ObjectSchema(
            description=(
                "Рекурсивно перечислить все файлы под директорией. Плоский "
                "список путей. При переполнении limit ответ обрезается с "
                "маркером '(truncated at limit=N)'. Для одного уровня — ls."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Корень обхода. Без значения — корень workspace.",
                    converter=Nullable(ChainConverter(IsString(), NonEmpty())),
                ),
                FieldSpec(
                    name="limit",
                    description="Максимум путей в ответе.",
                    converter=ChainConverter(IsInt(), MinValue(1)),
                    required=True,
                ),
            ],
            factory=TreeArgs,
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
