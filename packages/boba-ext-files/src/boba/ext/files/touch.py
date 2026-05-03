"""Tool: создать пустой файл или обновить mtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
from boba.validators import ChainConverter, Default, IsString, NonEmpty, ParseString
from boba.workspace import (
    WorkspaceError,
)


@dataclass(frozen=True)
class TouchArgs:
    path: str


@dataclass(frozen=True)
class TouchToolConfig:
    """DTO секции [ext.files.tools.touch]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class TouchTool(Tool[TouchArgs]):
    """Создать пустой файл или обновить mtime существующего."""

    _ID = ToolId("touch")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Создать пустой файл (включая промежуточные директории). "
        "Если уже существует — обновить время модификации, "
        "содержимое не трогать."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь к файлу."

    def __init__(self, cfg: TouchToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[TouchArgs]:
        p = self._cfg.params
        return ObjectSchema(
            description=self._cfg.description,
            fields=[
                FieldSpec(
                    name="path",
                    description=param_desc(p, "path", self.DEFAULT_PATH_DESC),
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
            ],
            factory=TouchArgs,
        )

    def execute(self, ctx: ToolContext, req: TouchArgs) -> ToolResult:
        try:
            ctx.project_workspace.touch(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка touch: {e}",
            ) from e
        return ToolResult(content=f"touch: {req.path}")


class TouchToolSection(ConfigSection[TouchToolConfig]):
    """Секция [ext.files.tools.touch]."""

    id: ClassVar[StrId] = StrId("ext.files.tools.touch")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "touch")

    schema: ClassVar[ObjectSchema[TouchToolConfig]] = ObjectSchema(
        description="Конфиг tool 'touch'.",
        fields=[
            FieldSpec(
                name="description",
                converter=ChainConverter(
                    Default(TouchTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=TouchToolConfig,
    )
