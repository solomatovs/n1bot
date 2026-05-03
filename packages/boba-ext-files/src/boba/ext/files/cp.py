"""Tool: копирование файла или директории (cp / cp -r)."""

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
class CpArgs:
    src: str
    dst: str
    recursive: bool


@dataclass(frozen=True)
class CpToolConfig:
    """DTO секции [ext.files.tools.cp]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class CpTool(Tool[CpArgs]):
    """Скопировать файл или директорию."""

    _ID = ToolId("cp")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Скопировать файл или директорию. Для директорий "
        "требуется recursive=true."
    )
    DEFAULT_SRC_DESC: ClassVar[str] = "Путь источника."
    DEFAULT_DST_DESC: ClassVar[str] = "Путь назначения."
    DEFAULT_RECURSIVE_DESC: ClassVar[str] = (
        "Рекурсивное копирование директории. По умолчанию false."
    )

    def __init__(self, cfg: CpToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[CpArgs]:
        p = self._cfg.params
        return ObjectSchema(
            description=self._cfg.description,
            fields=[
                FieldSpec(
                    name="src",
                    description=param_desc(p, "src", self.DEFAULT_SRC_DESC),
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="dst",
                    description=param_desc(p, "dst", self.DEFAULT_DST_DESC),
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
            factory=CpArgs,
        )

    def execute(self, ctx: ToolContext, req: CpArgs) -> ToolResult:
        try:
            ctx.project_workspace.copy(req.src, req.dst, recursive=req.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Источник не найден: {req.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка копирования: {e}",
            ) from e
        return ToolResult(content=f"Скопировано: {req.src} → {req.dst}")


class CpToolSection(ConfigSection[CpToolConfig]):
    """Секция [ext.files.tools.cp]."""

    id: ClassVar[StrId] = StrId("ext.files.tools.cp")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "cp")

    schema: ClassVar[ObjectSchema[CpToolConfig]] = ObjectSchema(
        description="Конфиг tool 'cp'.",
        fields=[
            FieldSpec(
                name="description",
                converter=ChainConverter(
                    Default(CpTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=CpToolConfig,
    )
