"""Tool: показать текущую директорию."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId
from boba.tools import Tool, ToolContext, ToolId, ToolResult, ToolSourceId
from boba.coercion import ChainCoercer, Default, ParseString


@dataclass(frozen=True)
class PwdArgs:
    """Пустой набор аргументов — pwd ничего не принимает."""


@dataclass(frozen=True)
class PwdToolConfig:
    """DTO секции [ext.files.tools.pwd]."""

    description: str


class PwdTool(Tool[PwdArgs]):
    """Возвращает путь текущей директории."""

    _ID = ToolId("pwd")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = "Вернуть путь текущей директории."

    def __init__(self, cfg: PwdToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[PwdArgs]:
        return ObjectSchema(
            description=self._cfg.description,
            fields=[],
            factory=PwdArgs,
        )

    def execute(self, ctx: ToolContext, req: PwdArgs) -> ToolResult:
        del req
        return ToolResult(content=ctx.project_workspace.cwd)


class PwdToolSection(ConfigSection[PwdToolConfig]):
    """Секция [ext.files.tools.pwd]."""

    id: ClassVar[StrId] = StrId("ext.files.tools.pwd")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "pwd")

    schema: ClassVar[ObjectSchema[PwdToolConfig]] = ObjectSchema(
        description="Конфиг tool 'pwd'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(PwdTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
        ],
        factory=PwdToolConfig,
    )
