"""Tool: список элементов workspace без рекурсии."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Any

from boba.adapters.tools._workspace_arg import (
    WORKSPACE_PARAM_NAME,
    parse_workspace_arg,
    workspace_param_schema,
)
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
from boba.domain.core.workspace import (
    USER_WORKSPACE_KIND,
    AllowedWorkspacesSpec,
    WorkspaceError,
    WorkspaceKind,
    WorkspaceResolver,
)


@dataclass(frozen=True)
class LsArgs:
    workspace: WorkspaceKind = USER_WORKSPACE_KIND
    limit: int | None = None


class LsArgsConverter(Converter[dict[str, Any], LsArgs]):
    def __init__(self, allowed: AllowedWorkspacesSpec, tool_id: ToolId) -> None:
        self._allowed = allowed
        self._tool_id = tool_id

    def convert(self, value: dict[str, Any]) -> LsArgs:
        limit = value.get("limit")
        return LsArgs(
            workspace=parse_workspace_arg(
                value.get(WORKSPACE_PARAM_NAME), self._allowed, self._tool_id
            ),
            limit=int(limit) if limit is not None else None,
        )


class LsTool(Tool[LsArgs]):
    """Плоский список элементов workspace (без рекурсии)."""

    _ID = ToolId("ls")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(
        self,
        resolver: WorkspaceResolver,
        allowed: AllowedWorkspacesSpec,
    ) -> None:
        self._resolver = resolver
        self._allowed = allowed

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def args_converter(self) -> Converter[dict[str, Any], LsArgs]:
        return LsArgsConverter(self._allowed, self._ID)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Показать элементы workspace на верхнем уровне (без рекурсии, "
                "без фильтрации). Порядок — как возвращает ФС, сортировка "
                "не применяется."
            ),
            input_schema=ToolInputSchema(
                params=[
                    workspace_param_schema(self._allowed),
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

        workspace = self._resolver.resolve(args.workspace)
        try:
            iterator = workspace.ls()
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
            return ToolResult(
                content=f"Workspace {args.workspace.name} пуст."
            )

        header = f"Элементы {args.workspace.name} ({len(items)}"
        if args.limit is not None:
            header += f", лимит={args.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"{header}\n{body}")
