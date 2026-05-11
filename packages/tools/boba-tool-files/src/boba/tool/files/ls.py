"""Tool: список элементов workspace без рекурсии."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import MinValue, NonEmpty
from boba.tools.domain import (
    TextResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError

__all__ = ["LsArgs", "LsTool", "LsToolConfig"]


@dataclass(frozen=True)
class LsArgs:
    """Перечислить содержимое директории на одном уровне без рекурсии.

    При переполнении limit ответ обрезается с маркером '(truncated at limit=N)'.
    Для рекурсии — tree.
    """

    path: Annotated[
        str | None, "Путь директории. Без значения — корень workspace.", NonEmpty()
    ] = None
    limit: Annotated[
        int, "Максимум элементов в ответе. По умолчанию 200.", MinValue(1)
    ] = 200


@dataclass(frozen=True)
class LsToolConfig:
    prompt: PromptOverlay


class LsTool(Tool[LsArgs, LsToolConfig]):
    """Плоский список элементов workspace (без рекурсии)."""

    def execute(self, ctx: ToolContext, req: LsArgs) -> ToolResult:
        try:
            iterator = ctx.project_workspace.ls(req.path)
            items = list(islice(iterator, req.limit + 1))
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(), message=f"Ошибка обхода: {e}",
            ) from e

        truncated = len(items) > req.limit
        if truncated:
            items = items[: req.limit]

        location = req.path or "/"

        if not items:
            return TextResult(text=f"{location} пуст.")

        header = f"Элементы {location} ({len(items)}, лимит={req.limit}"
        if truncated:
            header += f", truncated at limit={req.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return TextResult(text=f"{header}\n{body}")
