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
    ToolOutputTooLargeError,
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


_MAX_LINES = 2000


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
                "Прочитать текстовое содержимое файла. Возвращает файл "
                "целиком либо диапазон строк (1-based, включительно) через "
                "start_line/end_line. Не подходит для бинарных файлов. Если "
                "файла нет — возвращает ошибку 'Файл не найден'.\n"
                "\n"
                f"ЖЁСТКИЙ ЛИМИТ: за один вызов cat возвращает не более "
                f"{_MAX_LINES} строк. Лимит встроен в инструмент и не "
                "настраивается. Если запрошенный диапазон (или весь файл "
                "при отсутствии диапазона) превышает лимит — вместо "
                "данных будет типизированная ошибка "
                "'ToolOutputTooLargeError', вызов НЕ вернёт частичный "
                "результат.\n"
                "\n"
                "Как НЕ надо: звать cat без start_line/end_line на "
                "заведомо больших файлах (логи, сгенерённые артефакты, "
                "большие исходники) в надежде 'посмотреть целиком'.\n"
                "Как надо: если не уверен в размере — сначала stat, затем "
                f"пагинируй через start_line/end_line шагами ≤ {_MAX_LINES} "
                "строк. Если ищешь конкретный фрагмент — используй grep, "
                "а cat вызывай уже с прицельным диапазоном вокруг "
                "найденной строки."
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
        start = args.start_line or 1
        try:
            with self._workspace.read_text(args.filename, args.encoding) as f:
                text, last, overflow = self._read_range(
                    f, start, args.end_line, _MAX_LINES,
                )
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Файл не найден: {args.filename}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка чтения: {e}"
            ) from e

        if overflow:
            next_start = last + 1
            if args.end_line is not None:
                hint = (
                    f"Запрошенный диапазон {start}-{args.end_line} шире "
                    f"лимита. Читай его частями: следующий кусок — "
                    f"start_line={next_start}, end_line в пределах "
                    f"{next_start + _MAX_LINES - 1}."
                )
            else:
                hint = (
                    f"Файл {args.filename!r} длиннее лимита. Используй "
                    f"stat для размера и читай пагинированно: "
                    f"start_line={next_start}, "
                    f"end_line={next_start + _MAX_LINES - 1}. "
                    "Если нужна не сплошная прокрутка, а конкретный "
                    "фрагмент — сначала grep."
                )
            raise ToolOutputTooLargeError(
                tool_id=self._ID,
                limit=_MAX_LINES,
                unit="строк",
                hint=hint,
            )

        label = f"{args.filename}:{start}-{last}" if ranged else args.filename
        return ToolResult(content=f"### {label}\n\n{text}")

    @staticmethod
    def _read_range(
        f: TextIOBase, start: int, end: int | None, max_lines: int,
    ) -> tuple[str, int, bool]:
        """Стримит файл построчно, собирает только строки ``[start, end]``.

        Ранние строки читаются и отбрасываются (иначе позицию в файле не
        найти), хвост после ``end`` не читается вовсе — обрываем итерацию.
        Собирает не более ``max_lines`` строк; если в диапазоне есть ещё
        данные сверх лимита, возвращает ``overflow=True`` и номер последней
        собранной строки, чтобы caller мог подсказать LLM следующий
        start_line. Если диапазон пуст, ``last = start - 1``.
        """
        collected: list[str] = []
        last = start - 1
        overflow = False
        for i, line in enumerate(f, start=1):
            if i < start:
                continue
            if end is not None and i > end:
                break
            if len(collected) >= max_lines:
                overflow = True
                break
            collected.append(line.rstrip("\r\n"))
            last = i
        return "\n".join(collected), last, overflow
