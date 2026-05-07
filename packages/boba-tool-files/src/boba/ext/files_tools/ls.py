"""Tool: список элементов workspace без рекурсии."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    Nullable,
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
    ToolSourceId,
    ToolResult,
)
from boba.workspace import WorkspaceError

__all__ = ["LsTool", "LsToolConfig"]


@dataclass(frozen=True)
class LsArgs:
    path: str | None
    limit: int


@dataclass(frozen=True)
class LsToolConfig:
    prompt: PromptOverlay


class LsTool(Tool[LsArgs]):
    """Плоский список элементов workspace (без рекурсии)."""

    _NAME: ClassVar[ToolName] = ToolName("ls")

    def __init__(self, cfg: LsToolConfig, ctx: ExtensionContext, source_id: ToolSourceId) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[LsArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Перечислить содержимое директории на одном уровне без рекурсии. "
                "При переполнении limit ответ обрезается с маркером "
                "'(truncated at limit=N)'. Для рекурсии — tree."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description=(
                        "Путь директории. Без значения — корень workspace."
                    ),
                    coercer=Nullable(ChainCoercer(IsString(), NonEmpty())),
                ),
                FieldSpec(
                    name="limit",
                    description="Максимум элементов в ответе. По умолчанию 200.",
                    coercer=ChainCoercer(Default(200), IsInt(), MinValue(1)),
                ),
            ],
            factory=LsArgs,
        ))

    def execute(self, ctx: ToolContext, req: LsArgs) -> ToolResult:
        try:
            iterator = ctx.project_workspace.ls(req.path)
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

        header = f"Элементы {location} ({len(items)}, лимит={req.limit}"
        if truncated:
            header += f", truncated at limit={req.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return TextResult(text=f"{header}\n{body}")
