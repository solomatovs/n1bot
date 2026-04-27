"""Tool: список элементов workspace без рекурсии."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainConverter,
    FieldSpec,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    ObjectSchema,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
)


@dataclass(frozen=True)
class LsArgs:
    path: str | None
    limit: int


class LsArgsConverter(Converter[dict[str, Any], LsArgs]):
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

    def definition(self) -> ObjectSchema[dict[str, Any]]:
        return ObjectSchema(
            description=(
                "Перечислить содержимое директории на одном уровне без рекурсии. "
                "При переполнении limit ответ обрезается с маркером "
                "'(truncated at limit=N)'. Для рекурсии — tree."
            ),
            fields=[
                    FieldSpec(
                        name="path",
                        description="Путь директории. Без значения — корень workspace.",
                        converter=ChainConverter(IsString(), NonEmpty()),
                    ),
                    FieldSpec(
                        name="limit",
                        description="Максимум элементов в ответе.",
                        converter=ChainConverter(Required(), IsInt(), MinValue(1)),
                    ),
                ],
                invariants=Pass()
        )

    def execute(self, ctx: ToolContext, req: LsArgs) -> ToolResult:
        try:
            iterator = ctx.project_workspace.ls(req.path)
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

        header = f"Элементы {location} ({len(items)}, лимит={req.limit}"
        if truncated:
            header += f", truncated at limit={req.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"{header}\n{body}")

