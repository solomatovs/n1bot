"""Tool: удаление файла или директории (rm / rm -r)."""

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
from boba.validators import (
    ChainConverter,
    Default,
    IsBool,
    IsString,
    NonEmpty,
    ParseString,
)
from boba.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class RmArgs:
    path: str
    recursive: bool


@dataclass(frozen=True)
class RmToolConfig:
    """DTO секции [ext.files.tools.rm]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class RmTool(Tool[RmArgs]):
    """Удалить файл или директорию."""

    _ID = ToolId("rm")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Удалить файл или директорию. Для директорий требуется "
        "recursive=true. Безвозвратно."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь к файлу или директории."
    DEFAULT_RECURSIVE_DESC: ClassVar[str] = (
        "Удалить директорию со всем содержимым. По умолчанию false."
    )

    def __init__(self, cfg: RmToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[RmArgs]:
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
                FieldSpec(
                    name="recursive",
                    description=param_desc(
                        p, "recursive", self.DEFAULT_RECURSIVE_DESC
                    ),
                    converter=ChainConverter(Default(False), IsBool()),
                ),
            ],
            factory=RmArgs,
        )

    def execute(self, ctx: ToolContext, req: RmArgs) -> ToolResult:
        try:
            ctx.project_workspace.delete(req.path, recursive=req.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Не найдено: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка удаления: {e}",
            ) from e
        return ToolResult(content=f"Удалено: {req.path}")


class RmToolSection(ConfigSection[RmToolConfig]):
    """Секция [ext.files.tools.rm]."""

    id: ClassVar[StrId] = StrId("ext.files.tools.rm")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "rm")

    schema: ClassVar[ObjectSchema[RmToolConfig]] = ObjectSchema(
        description="Конфиг tool 'rm'.",
        fields=[
            FieldSpec(
                name="description",
                converter=ChainConverter(
                    Default(RmTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=RmToolConfig,
    )
