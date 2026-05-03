"""Tool: чтение содержимого файла (целиком или диапазон строк)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from io import TextIOBase
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
    ToolOutputTooLargeError,
    ToolResult,
    ToolSourceId,
    param_desc,
    params_field,
)
from boba.coercion import (
    ChainCoercer,
    Default,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    Ordered,
    ParseInt,
    ParseString,
)
from boba.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class CatArgs:
    path: str
    encoding: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class CatToolConfig:
    """DTO секции [ext.files.tools.cat]."""

    description: str
    max_lines: int
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class CatTool(Tool[CatArgs]):
    """Чтение содержимого файла (целиком или диапазон строк 1-based)."""

    _ID = ToolId("cat")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Прочитать строки [start_line; end_line] из текстового файла."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь к файлу."
    DEFAULT_ENCODING_DESC: ClassVar[str] = "Кодировка файла. По умолчанию 'utf-8'."
    DEFAULT_START_LINE_DESC: ClassVar[str] = "Первая строка окна. 1 = начало файла."
    DEFAULT_END_LINE_DESC: ClassVar[str] = (
        "Последняя строка окна, включительно. >= start_line."
    )
    DEFAULT_MAX_LINES: ClassVar[int] = 2000

    def __init__(self, cfg: CatToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[CatArgs]:
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
                    name="encoding",
                    description=param_desc(
                        p, "encoding", self.DEFAULT_ENCODING_DESC
                    ),
                    coercer=ChainCoercer(
                        Default("utf-8"), IsString(), NonEmpty()
                    ),
                ),
                FieldSpec(
                    name="start_line",
                    description=param_desc(
                        p, "start_line", self.DEFAULT_START_LINE_DESC
                    ),
                    coercer=ChainCoercer(IsInt(), MinValue(1)),
                    required=True,
                ),
                FieldSpec(
                    name="end_line",
                    description=param_desc(
                        p, "end_line", self.DEFAULT_END_LINE_DESC
                    ),
                    coercer=ChainCoercer(IsInt(), MinValue(1)),
                    required=True,
                ),
            ],
            invariants=Ordered("start_line", "end_line"),
            factory=CatArgs,
        )

    def execute(self, ctx: ToolContext, req: CatArgs) -> ToolResult:
        max_lines = self._cfg.max_lines
        if req.end_line - req.start_line + 1 > max_lines:
            raise ToolOutputTooLargeError(
                tool_id=self._ID,
                limit=max_lines,
                unit="строк",
                hint=(
                    f"Запрошенный диапазон "
                    f"{req.start_line}-{req.end_line} шире лимита. "
                    f"Читай окнами ≤ {max_lines} строк: "
                    f"start_line={req.start_line}, "
                    f"end_line={req.start_line + max_lines - 1}."
                ),
            )

        try:
            with ctx.project_workspace.read_text(req.path, req.encoding) as f:
                text, last = self._read_range(f, req.start_line, req.end_line)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Файл не найден: {req.path}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка чтения: {e}"
            ) from e

        label = f"{req.path}:{req.start_line}-{last}"
        return ToolResult(content=f"### {label}\n\n{text}")

    @staticmethod
    def _read_range(
        f: TextIOBase,
        start: int,
        end: int,
    ) -> tuple[str, int]:
        """Стримит файл построчно, возвращая только строки [start, end]."""
        collected: list[str] = []
        last = start - 1
        for i, line in enumerate(f, start=1):
            if i < start:
                continue
            if i > end:
                break
            collected.append(line.rstrip("\r\n"))
            last = i
        return "\n".join(collected), last


class CatToolSection(ConfigSection[CatToolConfig]):
    """Секция [ext.files.tools.cat]: описания + лимиты cat-tool'а."""

    id: ClassVar[StrId] = StrId("ext.files.tools.cat")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "cat")

    schema: ClassVar[ObjectSchema[CatToolConfig]] = ObjectSchema(
        description="Конфиг tool 'cat': описания + потолок строк.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(CatTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            FieldSpec(
                name="max_lines",
                coercer=ChainCoercer(
                    Default(CatTool.DEFAULT_MAX_LINES), ParseInt(), MinValue(1)
                ),
                description=(
                    "Максимум строк в одном вызове cat (защита от "
                    "переполнения ответа)."
                ),
            ),
            params_field("params"),
        ],
        factory=CatToolConfig,
    )
