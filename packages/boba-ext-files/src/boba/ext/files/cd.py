"""Tool: сменить текущую директорию."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, IsString, NonEmpty, ParseString
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
from boba.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class CdArgs:
    path: str


@dataclass(frozen=True)
class CdToolConfig:
    """DTO секции [ext.files.tools.cd]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class CdTool(Tool[CdArgs]):
    """Сменить текущую директорию."""

    _ID = ToolId("cd")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = "Сменить текущую директорию."
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь директории."

    def __init__(self, cfg: CdToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[CdArgs]:
        p = self._cfg.params
        return ObjectSchema(
            description=self._cfg.description,
            fields=[
                FieldSpec(
                    name="path",
                    description=param_desc(p, "path", self.DEFAULT_PATH_DESC),
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
            ],
            factory=CdArgs,
        )

    def execute(self, ctx: ToolContext, req: CdArgs) -> ToolResult:
        try:
            ctx.project_workspace.cd(req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Директория не найдена: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка cd: {e}",
            ) from e
        return ToolResult(content=f"Текущая директория: {ctx.project_workspace.cwd}")


class CdToolSection(ConfigSection[CdToolConfig]):
    """Секция [ext.files.tools.cd]."""

    id: ClassVar[StrId] = StrId("ext.files.tools.cd")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "cd")

    schema: ClassVar[ObjectSchema[CdToolConfig]] = ObjectSchema(
        description="Конфиг tool 'cd'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(CdTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=CdToolConfig,
    )
