"""Tool: чтение содержимого файла (целиком или диапазон строк)."""

from __future__ import annotations

from dataclasses import dataclass
from io import TextIOBase
from typing import Any

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
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class CatArgs:
    filename: str
    encoding: str
    start_line: int | None = None
    end_line: int | None = None


class CatArgsConverter(Converter[dict[str, Any], CatArgs]):
    """Маппит провалидированный dict в :class:`CatArgs`."""

    def convert(self, value: dict[str, Any]) -> CatArgs:
        return CatArgs(
            filename=value["filename"],
            encoding=value["encoding"],
            start_line=value.get("start_line"),
            end_line=value.get("end_line"),
        )


class CatTool(Tool[CatArgs]):
    """Чтение содержимого файла (целиком или диапазон строк 1-based)."""

    _ID = ToolId("cat")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: ProjectWorkspaceShell) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], CatArgs]:
        return CatArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Прочитать текстовое содержимое файла. По умолчанию "
                "возвращает файл целиком; через start_line/end_line можно "
                "запросить диапазон строк (1-based, включительно). Не "
                "подходит для бинарных файлов. Если файла нет — возвращает "
                "ошибку 'Файл не найден'."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="filename",
                        description="Путь к файлу.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="encoding",
                        description=(
                            "Текстовая кодировка файла, например 'utf-8' "
                            "или 'cp1251'. По умолчанию — 'utf-8'."
                        ),
                        validator=ChainValidator(
                            Default("utf-8"), IsString(), NonEmpty()
                        ),
                    ),
                    ParamSchema(
                        name="start_line",
                        description=(
                            "Начальная строка диапазона; 1 — первая строка. "
                            "Без end_line читается до конца файла. Опционально."
                        ),
                        validator=ChainValidator(IsInt(), MinValue(1)),
                    ),
                    ParamSchema(
                        name="end_line",
                        description=(
                            "Конечная строка диапазона, включительно. Без "
                            "start_line читается с первой строки. Должна "
                            "быть >= start_line. Опционально."
                        ),
                        validator=ChainValidator(IsInt(), MinValue(1)),
                    ),
                ],
                invariants=Ordered("start_line", "end_line"),
            ),
        )

    def execute(self, ctx: None, args: CatArgs) -> ToolResult:
        ranged = args.start_line is not None or args.end_line is not None
        try:
            with self._workspace.read_text(args.filename, args.encoding) as f:
                if ranged:
                    start = args.start_line or 1
                    text, last = self._read_range(f, start, args.end_line)
                    label = f"{args.filename}:{start}-{last}"
                else:
                    text = f.read()
                    label = args.filename
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Файл не найден: {args.filename}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка чтения: {e}"
            ) from e

        return ToolResult(content=f"### {label}\n\n{text}")

    @staticmethod
    def _read_range(
        f: TextIOBase, start: int, end: int | None,
    ) -> tuple[str, int]:
        """Стримит файл построчно, собирает только строки ``[start, end]``.

        Ранние строки читаются и отбрасываются (иначе позицию в файле не
        найти), хвост после ``end`` не читается вовсе — обрываем итерацию.
        Возвращает склеенный через ``\\n`` текст и номер последней реально
        прочитанной строки (равен ``start - 1`` если диапазон пуст).
        """
        collected: list[str] = []
        last = start - 1
        for i, line in enumerate(f, start=1):
            if i < start:
                continue
            if end is not None and i > end:
                break
            collected.append(line.rstrip("\r\n"))
            last = i
        return "\n".join(collected), last
