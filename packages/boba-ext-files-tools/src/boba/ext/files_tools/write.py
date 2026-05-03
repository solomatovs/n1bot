"""Tool: перезаписать файл целиком."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    IsString,
    NonEmpty,
    ParseString,
)
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
class WriteArgs:
    path: str
    content: str
    encoding: str


@dataclass(frozen=True)
class WriteToolConfig:
    """DTO секции [ext.files.tools.write]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class WriteTool(Tool[WriteArgs]):
    """Полностью перезаписать файл содержимым."""

    _ID = ToolId("write")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Перезаписать файл указанным содержимым. Если файла или "
        "промежуточных директорий нет — создать."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь к файлу."
    DEFAULT_CONTENT_DESC: ClassVar[str] = "Новое содержимое файла."
    DEFAULT_ENCODING_DESC: ClassVar[str] = "Кодировка файла. По умолчанию 'utf-8'."

    def __init__(self, cfg: WriteToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[WriteArgs]:
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
                FieldSpec(
                    name="content",
                    description=param_desc(
                        p, "content", self.DEFAULT_CONTENT_DESC
                    ),
                    coercer=ChainCoercer(IsString()),
                    required=True,
                ),
                FieldSpec(
                    name="encoding",
                    description=param_desc(
                        p, "encoding", self.DEFAULT_ENCODING_DESC
                    ),
                    coercer=ChainCoercer(
                        Default("utf-8"),
                        IsString(),
                        NonEmpty(),
                    ),
                ),
            ],
            factory=WriteArgs,
        )

    def execute(self, ctx: ToolContext, req: WriteArgs) -> ToolResult:
        existed = ctx.project_workspace.exists(req.path)
        try:
            with ctx.project_workspace.write_text(req.path, req.encoding) as f:
                f.write(req.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка записи: {e}",
            ) from e
        action = "обновлён" if existed else "создан"
        return TextResult(text=f"Файл {action}: {req.path} ({len(req.content)} символов)",
        )


class WriteToolSection(ConfigSection[WriteToolConfig]):
    """Секция [ext.files.tools.write]."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "write")

    schema: ClassVar[ObjectSchema[WriteToolConfig]] = ObjectSchema(
        description="Конфиг tool 'write'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(WriteTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=WriteToolConfig,
    )
