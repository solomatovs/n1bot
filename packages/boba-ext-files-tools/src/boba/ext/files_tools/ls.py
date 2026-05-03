"""Tool: список элементов workspace без рекурсии."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
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
from boba.workspace import (
    WorkspaceError,
)


@dataclass(frozen=True)
class LsArgs:
    path: str | None
    limit: int


@dataclass(frozen=True)
class LsToolConfig:
    """DTO секции [ext.files.tools.ls]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class LsTool(Tool[LsArgs]):
    """Плоский список элементов workspace (без рекурсии)."""

    _ID = ToolId("ls")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Перечислить содержимое директории на одном уровне без рекурсии. "
        "При переполнении limit ответ обрезается с маркером "
        "'(truncated at limit=N)'. Для рекурсии — tree."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = (
        "Путь директории. Без значения — корень workspace."
    )
    DEFAULT_LIMIT_DESC: ClassVar[str] = (
        "Максимум элементов в ответе. По умолчанию 200."
    )

    def __init__(self, cfg: LsToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[LsArgs]:
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
                    coercer=ChainCoercer(Default(200), IsInt(), MinValue(1)),
                ),
            ],
            factory=LsArgs,
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


class LsToolSection(ConfigSection[LsToolConfig]):
    """Секция [ext.files.tools.ls]."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "ls")

    schema: ClassVar[ObjectSchema[LsToolConfig]] = ObjectSchema(
        description="Конфиг tool 'ls'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(LsTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=LsToolConfig,
    )
