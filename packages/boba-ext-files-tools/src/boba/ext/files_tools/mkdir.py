"""Tool: создать директорию."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, IsString, NonEmpty, ParseString
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.tools.domain import (
    ParamOverlay,
    TextResult,
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
class MkdirArgs:
    path: str


@dataclass(frozen=True)
class MkdirToolConfig:
    """DTO секции [ext.files.tools.mkdir]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class MkdirTool(Tool[MkdirArgs]):
    """Создать директорию."""

    _ID = ToolId("mkdir")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Создать директорию (включая промежуточные). Если уже "
        "существует — no-op. Если по пути файл — ошибка."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь создаваемой директории."

    def __init__(self, cfg: MkdirToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[MkdirArgs]:
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
            factory=MkdirArgs,
        )

    def execute(self, ctx: ToolContext, req: MkdirArgs) -> ToolResult:
        try:
            ctx.project_workspace.mkdir(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка mkdir: {e}",
            ) from e
        return TextResult(text=f"Директория создана: {req.path}")


class MkdirToolSection(ConfigSection[MkdirToolConfig]):
    """Секция [ext.files.tools.mkdir]."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "mkdir")

    schema: ClassVar[ObjectSchema[MkdirToolConfig]] = ObjectSchema(
        description="Конфиг tool 'mkdir'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(MkdirTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=MkdirToolConfig,
    )
