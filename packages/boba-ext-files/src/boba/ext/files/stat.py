"""Tool: метаданные файла или директории."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, IsString, NonEmpty, ParseString
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
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class StatArgs:
    path: str


@dataclass(frozen=True)
class StatToolConfig:
    """DTO секции [ext.files.tools.stat]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class StatTool(Tool[StatArgs]):
    """Метаданные файла или директории."""

    _ID = ToolId("stat")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Вернуть метаданные ресурса: тип (file/directory/other), "
        "размер в байтах, время модификации. Если ресурса нет — "
        "ошибка. Для директорий size — размер inode-блока ФС, не "
        "количество файлов; для содержимого директории — ls/tree."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь к файлу или директории."

    def __init__(self, cfg: StatToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[StatArgs]:
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
            factory=StatArgs,
        )

    def execute(self, ctx: ToolContext, req: StatArgs) -> ToolResult:
        try:
            meta = ctx.project_workspace.meta(req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Не найдено: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка stat: {e}",
            ) from e

        body = (
            f"path: {meta.path}\n"
            f"kind: {meta.kind}\n"
            f"size: {meta.size}\n"
            f"modified: {meta.modified.isoformat()}"
        )
        return ToolResult(content=body)


class StatToolSection(ConfigSection[StatToolConfig]):
    """Секция [ext.files.tools.stat]."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "stat")

    schema: ClassVar[ObjectSchema[StatToolConfig]] = ObjectSchema(
        description="Конфиг tool 'stat'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(StatTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=StatToolConfig,
    )
