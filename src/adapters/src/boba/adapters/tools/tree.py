"""Tool: рекурсивный обход workspace."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Any

from boba.adapters.tools._workspace_arg import WorkspaceScopedToolId
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
    WorkspaceError,
    WorkspaceKind,
    WorkspaceResolver,
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

    _BASE_NAME = "tree"
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(
        self,
        resolver: WorkspaceResolver,
        workspace: WorkspaceKind,
    ) -> None:
        self._resolver = resolver
        self._workspace = workspace
        self._id = WorkspaceScopedToolId.build(workspace, self._BASE_NAME)

    def tool_id(self) -> ToolId:
        return self._id

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], TreeArgs]:
        return TreeArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                f"Рекурсивно показать все файлы workspace "
                f"'{self._workspace.name}', включая содержимое всех вложенных "
                "директорий. Возвращает плоский список путей в порядке "
                "файловой системы, без сортировки. Для одного уровня "
                "вложенности используй tool 'ls'."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description=(
                            "Корневая поддиректория обхода относительно корня "
                            "workspace. Без него — обход всего workspace."
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
        workspace = self._resolver.resolve(self._workspace)
        try:
            iterator = workspace.tree(args.path)
            items = (
                list(iterator)
                if args.limit is None
                else list(islice(iterator, args.limit))
            )
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._id, message=f"Ошибка обхода workspace: {e}"
            ) from e

        location = self._workspace.name
        if args.path:
            location += f":{args.path}"

        if not items:
            return ToolResult(content=f"{location} пуст.")

        header = f"Файлы {location} ({len(items)}"
        if args.limit is not None:
            header += f", лимит={args.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"{header}\n{body}")
