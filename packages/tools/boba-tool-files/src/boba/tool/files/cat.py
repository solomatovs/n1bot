"""Tool: чтение содержимого файла (целиком или диапазон строк)."""

from __future__ import annotations

from io import TextIOBase
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.plugin.prompt import PromptOverlay
from boba.tool.files._base import FsToolBase
from boba.tools.domain import (
    JsonResult,
    ToolContext,
    ToolExecutionError,
    ToolOutputTooLargeError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError, WorkspaceNotFoundError

__all__ = ["CatArgs", "CatTool", "CatToolConfig"]


class CatArgs(BaseModel):
    """Прочитать строки [start_line; end_line] из текстового файла."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Путь к файлу.")
    start_line: int = Field(
        ge=1,
        description="Первая строка окна. 1 = начало файла.",
    )
    end_line: int = Field(
        ge=1,
        description="Последняя строка окна, включительно >= start_line.",
    )
    encoding: str = Field(
        default="utf-8",
        min_length=1,
        description="Кодировка файла. По умолчанию 'utf-8'.",
    )

    @model_validator(mode="after")
    def _check_ordered(self) -> Self:
        if self.start_line > self.end_line:
            msg = (
                f"start_line ({self.start_line}) должна быть "
                f"<= end_line ({self.end_line})"
            )
            raise ValueError(msg)
        return self


class CatToolConfig(BaseModel):
    """Конфиг tool 'cat': лимит max_lines + prompt overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_lines: int = Field(
        default=2000,
        ge=1,
        description="Максимум строк в одном вызове cat.",
    )
    prompt: PromptOverlay = Field(default_factory=PromptOverlay)


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
            with self._shell.read_text(req.path, req.encoding) as f:
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

        return JsonResult(payload={
            "path": req.path,
            "start_line": req.start_line,
            "end_line": last,
            "content": text,
        })

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
