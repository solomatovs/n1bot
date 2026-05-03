"""Tool: рекурсивный обход workspace."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import islice
from typing import ClassVar

from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId
from boba.tools import (
    ParamOverlay,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
    param_desc,
    params_field,
)
from boba.coercion import (
    ChainCoercer,
    Default,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    Nullable,
    ParseString,
)
from boba.workspace import (
    WorkspaceError,
)


@dataclass(frozen=True)
class TreeArgs:
    path: str | None
    limit: int


@dataclass(frozen=True)
class TreeToolConfig:
    """DTO секции [ext.files.tools.tree]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class TreeTool(Tool[TreeArgs]):
    """Рекурсивный обход всех файлов workspace."""

    _ID = ToolId("tree")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Рекурсивно перечислить все файлы под директорией. Плоский "
        "список путей. При переполнении limit ответ обрезается с "
        "маркером '(truncated at limit=N)'. Для одного уровня — ls."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = (
        "Корень обхода. Без значения — корень workspace."
    )
    DEFAULT_LIMIT_DESC: ClassVar[str] = "Максимум путей в ответе."

    def __init__(self, cfg: TreeToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[TreeArgs]:
        p = self._cfg.params
        return ObjectSchema(
            description=self._cfg.description,
            fields=[
                FieldSpec(
                    name="path",
                    description=param_desc(p, "path", self.DEFAULT_PATH_DESC),
                    coercer=Nullable(ChainCoercer(IsString(), NonEmpty())),
                ),
                FieldSpec(
                    name="limit",
                    description=param_desc(p, "limit", self.DEFAULT_LIMIT_DESC),
                    coercer=ChainCoercer(IsInt(), MinValue(1)),
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


class TreeToolSection(ConfigSection[TreeToolConfig]):
    """Секция [ext.files.tools.tree]."""

    id: ClassVar[StrId] = StrId("ext.files.tools.tree")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "tree")

    schema: ClassVar[ObjectSchema[TreeToolConfig]] = ObjectSchema(
        description="Конфиг tool 'tree'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(TreeTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=TreeToolConfig,
    )
