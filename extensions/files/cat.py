"""Tool: чтение содержимого файла (целиком или диапазон строк)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from io import TextIOBase
from typing import Any

from boba.adapters.tool_providers import StaticToolSource
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    Default,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    Ordered,
    ParamSchema,
    Required,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolOutputTooLargeError,
    ToolResult,
    ToolSource,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)
from boba.infra.extensions import ExtensionContext


@dataclass(frozen=True)
class CatArgs:
    path: str
    encoding: str
    start_line: int
    end_line: int


class CatArgsConverter(Converter[dict[str, Any], CatArgs]):
    """Маппит провалидированный dict в :class:`CatArgs`."""

    def convert(self, value: dict[str, Any]) -> CatArgs:
        return CatArgs(
            path=value["path"],
            encoding=value["encoding"],
            start_line=value["start_line"],
            end_line=value["end_line"],
        )


_MAX_LINES = 2000


class CatTool(Tool[CatArgs]):
    """Чтение содержимого файла (целиком или диапазон строк 1-based)."""

    _ID = ToolId("cat")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], CatArgs]:
        return CatArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description="Прочитать строки [start_line; end_line] из текстового файла.",
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Путь к файлу.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="encoding",
                        description="Кодировка файла. По умолчанию 'utf-8'.",
                        validator=ChainValidator(
                            Default("utf-8"), IsString(), NonEmpty()
                        ),
                    ),
                    ParamSchema(
                        name="start_line",
                        description="Первая строка окна. 1 = начало файла.",
                        validator=ChainValidator(Required(), IsInt(), MinValue(1)),
                    ),
                    ParamSchema(
                        name="end_line",
                        description=(
                            "Последняя строка окна, включительно. >= start_line."
                        ),
                        validator=ChainValidator(Required(), IsInt(), MinValue(1)),
                    ),
                ],
                invariants=Ordered("start_line", "end_line"),
            ),
        )

    def execute(self, ctx: ToolContext, args: CatArgs) -> ToolResult:
        if args.end_line - args.start_line + 1 > _MAX_LINES:
            raise ToolOutputTooLargeError(
                tool_id=self._ID,
                limit=_MAX_LINES,
                unit="строк",
                hint=(
                    f"Запрошенный диапазон "
                    f"{args.start_line}-{args.end_line} шире лимита. "
                    f"Читай окнами ≤ {_MAX_LINES} строк: "
                    f"start_line={args.start_line}, "
                    f"end_line={args.start_line + _MAX_LINES - 1}."
                ),
            )

        try:
            with ctx.project_workspace.read_text(args.path, args.encoding) as f:
                text, last = self._read_range(f, args.start_line, args.end_line)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Файл не найден: {args.path}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка чтения: {e}"
            ) from e

        label = f"{args.path}:{args.start_line}-{last}"
        return ToolResult(content=f"### {label}\n\n{text}")

    @staticmethod
    def _read_range(
        f: TextIOBase,
        start: int,
        end: int,
    ) -> tuple[str, int]:
        """Стримит файл построчно, собирает только строки ``[start, end]``.

        Ранние строки читаются и отбрасываются (иначе позицию в файле не
        найти), хвост после ``end`` не читается вовсе — обрываем итерацию.
        Если диапазон пуст, ``last = start - 1``.
        """
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

def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files.cat"),
        priority=0,
        tools=[CatTool()],
    )
