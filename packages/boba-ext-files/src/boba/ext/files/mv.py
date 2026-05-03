"""Tool: переместить/переименовать файл или директорию."""

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
class MvArgs:
    src: str
    dst: str


@dataclass(frozen=True)
class MvToolConfig:
    """DTO секции [ext.files.tools.mv]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class MvTool(Tool[MvArgs]):
    """Переместить/переименовать файл или директорию."""

    _ID = ToolId("mv")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Переместить или переименовать файл/директорию. Если dst — "
        "существующая директория, src переносится внутрь. Файл по "
        "пути dst перезаписывается. Промежуточные директории не "
        "создаются."
    )
    DEFAULT_SRC_DESC: ClassVar[str] = "Путь источника."
    DEFAULT_DST_DESC: ClassVar[str] = "Путь назначения."

    def __init__(self, cfg: MvToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[MvArgs]:
        p = self._cfg.params
        return ObjectSchema(
            description=self._cfg.description,
            fields=[
                FieldSpec(
                    name="src",
                    description=param_desc(p, "src", self.DEFAULT_SRC_DESC),
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="dst",
                    description=param_desc(p, "dst", self.DEFAULT_DST_DESC),
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
            ],
            factory=MvArgs,
        )

    def execute(self, ctx: ToolContext, req: MvArgs) -> ToolResult:
        try:
            ctx.project_workspace.move(req.src, req.dst)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Источник не найден: {req.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка перемещения: {e}",
            ) from e
        return ToolResult(content=f"Перемещено: {req.src} → {req.dst}")


class MvToolSection(ConfigSection[MvToolConfig]):
    """Секция [ext.files.tools.mv]."""

    id: ClassVar[StrId] = StrId("ext.files.tools.mv")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "mv")

    schema: ClassVar[ObjectSchema[MvToolConfig]] = ObjectSchema(
        description="Конфиг tool 'mv'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(MvTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=MvToolConfig,
    )
