"""Tool: find-and-replace редактирование файла."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    IsBool,
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
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class EditArgs:
    path: str
    old_string: str
    new_string: str
    replace_all: bool
    encoding: str


@dataclass(frozen=True)
class EditToolConfig:
    """DTO секции [ext.files.tools.edit]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class EditTool(Tool[EditArgs]):
    """Find-and-replace редактирование текстового файла."""

    _ID = ToolId("edit")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Заменить подстроку old_string на new_string. По умолчанию "
        "old_string должна встречаться в файле ровно один раз — "
        "иначе ошибка. С replace_all=true заменяются все вхождения. "
        "Совпадение точное, посимвольное."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь к файлу."
    DEFAULT_OLD_STRING_DESC: ClassVar[str] = "Подстрока для замены. Совпадение точное."
    DEFAULT_NEW_STRING_DESC: ClassVar[str] = "Заменяющий текст. Пустая строка = удаление."
    DEFAULT_REPLACE_ALL_DESC: ClassVar[str] = (
        "Заменить все вхождения. По умолчанию false."
    )
    DEFAULT_ENCODING_DESC: ClassVar[str] = "Кодировка файла. По умолчанию 'utf-8'."

    def __init__(self, cfg: EditToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[EditArgs]:
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
                    name="old_string",
                    description=param_desc(
                        p, "old_string", self.DEFAULT_OLD_STRING_DESC
                    ),
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="new_string",
                    description=param_desc(
                        p, "new_string", self.DEFAULT_NEW_STRING_DESC
                    ),
                    coercer=ChainCoercer(IsString()),
                    required=True,
                ),
                FieldSpec(
                    name="replace_all",
                    description=param_desc(
                        p, "replace_all", self.DEFAULT_REPLACE_ALL_DESC
                    ),
                    coercer=ChainCoercer(Default(False), IsBool()),
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
            factory=EditArgs,
        )

    def execute(self, ctx: ToolContext, req: EditArgs) -> ToolResult:
        try:
            applied = ctx.project_workspace.edit_text(
                req.path,
                req.old_string,
                req.new_string,
                replace_all=req.replace_all,
                encoding=req.encoding,
            )
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Файл не найден: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка edit: {e}",
            ) from e
        return TextResult(text=f"Заменено в {req.path}: {applied} вхождение(й).",
        )


class EditToolSection(ConfigSection[EditToolConfig]):
    """Секция [ext.files.tools.edit]."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "edit")

    schema: ClassVar[ObjectSchema[EditToolConfig]] = ObjectSchema(
        description="Конфиг tool 'edit'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(EditTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=EditToolConfig,
    )
