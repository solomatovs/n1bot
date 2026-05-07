"""Tool: чтение содержимого файла (целиком или диапазон строк)."""

from __future__ import annotations

from dataclasses import dataclass
from io import TextIOBase
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    Ordered,
)
from boba.declaration import FieldSpec, ObjectSchema
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import (
    TextResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolOutputTooLargeError,
    ToolResult,
    ToolSourceId,
)
from boba.workspace import WorkspaceError, WorkspaceNotFoundError

__all__ = ["CatTool", "CatToolConfig"]


@dataclass(frozen=True)
class CatArgs:
    path: str
    encoding: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class CatToolConfig:
    """DTO tool'а: runtime-лимит max_lines + prompt overlay."""

    max_lines: int
    prompt: PromptOverlay


class CatTool(Tool[CatArgs]):
    """Чтение содержимого файла (целиком или диапазон строк 1-based)."""

    _ID: ClassVar[ToolId] = ToolId("cat")
    _SOURCE: ClassVar[ToolSourceId] = ToolSourceId("plugin.files")

    def __init__(self, cfg: CatToolConfig, ctx: ExtensionContext) -> None:
        self._cfg = cfg
        self._ctx = ctx

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[CatArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Прочитать строки [start_line; end_line] из текстового файла."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу.",
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="encoding",
                    description="Кодировка файла. По умолчанию 'utf-8'.",
                    coercer=ChainCoercer(Default("utf-8"), IsString(), NonEmpty()),
                ),
                FieldSpec(
                    name="start_line",
                    description="Первая строка окна. 1 = начало файла.",
                    coercer=ChainCoercer(IsInt(), MinValue(1)),
                    required=True,
                ),
                FieldSpec(
                    name="end_line",
                    description="Последняя строка окна, включительно. >= start_line.",
                    coercer=ChainCoercer(IsInt(), MinValue(1)),
                    required=True,
                ),
            ],
            invariants=Ordered("start_line", "end_line"),
            factory=CatArgs,
        ))

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
                tool_id=self._ID, message=f"Файл не найден: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка чтения: {e}",
            ) from e

        label = f"{req.path}:{req.start_line}-{last}"
        return TextResult(text=f"### {label}\n\n{text}")

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
