"""Tool: рекурсивный обход workspace."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    Nullable,
    Required,
)
from boba.declaration import FieldSpec, ObjectSchema
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import (
    TextResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolName,
    ToolResult,
    ToolSourceId,
)
from boba.workspace.contract import WorkspaceError

__all__ = ["TreeTool", "TreeToolConfig"]


@dataclass(frozen=True)
class TreeArgs:
    path: str | None
    limit: int


@dataclass(frozen=True)
class TreeToolConfig:
    prompt: PromptOverlay


class TreeTool(Tool[TreeArgs]):
    """Рекурсивный обход всех файлов workspace."""

    _NAME: ClassVar[ToolName] = ToolName("tree")

    def __init__(self, cfg: TreeToolConfig, ctx: ExtensionContext, source_id: ToolSourceId) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[TreeArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Рекурсивно перечислить все файлы под директорией. Плоский "
                "список путей. При переполнении limit ответ обрезается с "
                "маркером '(truncated at limit=N)'. Для одного уровня — ls."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Корень обхода. Без значения — корень workspace.",
                    coercer=Nullable(ChainCoercer(IsString(), NonEmpty())),
                ),
                FieldSpec(
                    name="limit",
                    description="Максимум путей в ответе.",
                    coercer=ChainCoercer(Required(), IsInt(), MinValue(1)),
                ),
            ],
            factory=TreeArgs,
        ))

    def execute(self, ctx: ToolContext, req: TreeArgs) -> ToolResult:
        try:
            iterator = ctx.project_workspace.tree(req.path)
            items = list(islice(iterator, req.limit + 1))
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id, message=f"Ошибка обхода: {e}",
            ) from e

        truncated = len(items) > req.limit
        if truncated:
            items = items[: req.limit]

        location = req.path or "/"

        if not items:
            return TextResult(text=f"{location} пуст.")

        header = f"Файлы {location} ({len(items)}, лимит={req.limit}"
        if truncated:
            header += f", truncated at limit={req.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return TextResult(text=f"{header}\n{body}")
