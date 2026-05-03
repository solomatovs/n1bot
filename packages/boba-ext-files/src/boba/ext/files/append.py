"""Tool: дозаписать в конец файла."""

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
    IsString,
    NonEmpty,
    ParseString,
)
from boba.workspace import (
    WorkspaceError,
)


@dataclass(frozen=True)
class AppendArgs:
    path: str
    content: str
    encoding: str


@dataclass(frozen=True)
class AppendToolConfig:
    """DTO секции [ext.files.tools.append]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class AppendTool(Tool[AppendArgs]):
    """Дозаписать текст в конец файла."""

    _ID = ToolId("append")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Дописать текст в конец файла. Если файла нет — создать."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь к файлу."
    DEFAULT_CONTENT_DESC: ClassVar[str] = "Дописываемый текст."
    DEFAULT_ENCODING_DESC: ClassVar[str] = "Кодировка файла. По умолчанию 'utf-8'."

    def __init__(self, cfg: AppendToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[AppendArgs]:
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
                    name="content",
                    description=param_desc(
                        p, "content", self.DEFAULT_CONTENT_DESC
                    ),
                    converter=ChainConverter(IsString()),
                    required=True,
                ),
                FieldSpec(
                    name="encoding",
                    description=param_desc(
                        p, "encoding", self.DEFAULT_ENCODING_DESC
                    ),
                    converter=ChainConverter(
                        Default("utf-8"),
                        IsString(),
                        NonEmpty(),
                    ),
                ),
            ],
            factory=AppendArgs,
        )

    def execute(self, ctx: ToolContext, req: AppendArgs) -> ToolResult:
        existed = ctx.project_workspace.exists(req.path)
        try:
            with ctx.project_workspace.append_text(req.path, req.encoding) as f:
                f.write(req.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка записи: {e}",
            ) from e
        action = "дозаписан" if existed else "создан"
        return ToolResult(
            content=f"Файл {action}: {req.path} ({len(req.content)} символов)",
        )


class AppendToolSection(ConfigSection[AppendToolConfig]):
    """Секция [ext.files.tools.append]."""

    id: ClassVar[StrId] = StrId("ext.files.tools.append")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "append")

    schema: ClassVar[ObjectSchema[AppendToolConfig]] = ObjectSchema(
        description="Конфиг tool 'append'.",
        fields=[
            FieldSpec(
                name="description",
                converter=ChainConverter(
                    Default(AppendTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=AppendToolConfig,
    )
