"""Tool: чтение содержимого файла (целиком или диапазон строк)."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import TextIOBase
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema import schema
from boba.schema.coercion import MinValue, NonEmpty, Ordered, ParseInt
from boba.tool.files._base import FsToolBase
from boba.tools.domain import (
    TextResult,
    ToolContext,
    ToolExecutionError,
    ToolOutputTooLargeError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError, WorkspaceNotFoundError

__all__ = ["CatArgs", "CatTool", "CatToolConfig"]


@schema(invariants=Ordered("start_line", "end_line"))
@dataclass(frozen=True)
class CatArgs:
    """Прочитать строки [start_line; end_line] из текстового файла."""

    path: Annotated[str, "Путь к файлу.", NonEmpty()]
    start_line: Annotated[int, "Первая строка окна. 1 = начало файла.", MinValue(1)]
    end_line: Annotated[
        int,
        "Последняя строка окна, включительно >= start_line.",
        MinValue(1),
    ]
    encoding: Annotated[
        str,
        "Кодировка файла. По умолчанию 'utf-8'.",
        NonEmpty(),
    ] = "utf-8"


@dataclass(frozen=True)
class CatToolConfig:
    """Конфиг tool 'cat': лимит max_lines + prompt overlay."""

    max_lines: Annotated[
        int,
        "Максимум строк в одном вызове cat.",
        ParseInt(),
        MinValue(1),
    ] = 2000
    prompt: PromptOverlay = field(default_factory=PromptOverlay)


class CatTool(FsToolBase[CatArgs, CatToolConfig]):
    """Чтение содержимого файла (целиком или диапазон строк 1-based)."""

    def execute(self, ctx: ToolContext, req: CatArgs) -> ToolResult:
        max_lines = self._cfg.max_lines
        if req.end_line - req.start_line + 1 > max_lines:
            raise ToolOutputTooLargeError(
                tool_id=self.tool_id(),
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
            with self._shell(ctx).read_text(req.path, req.encoding) as f:
                text, last = self._read_range(f, req.start_line, req.end_line)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Файл не найден: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка чтения: {e}",
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
