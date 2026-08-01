"""ToolResult sealed-семейство: TextResult | JsonResult | TableResult | ... .

Порт boba.tools.domain.result для langchain-стека. Инструмент возвращает
ToolResult, а pack_result() из render.py превращает его в
(content для LLM, artifact для UI). UI-рендер (ToolResultView) диспатчит
по полю kind — новый вариант требует ветку и в render_for_llm, и в рендере.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Iterator, Mapping, Sequence
from typing import Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ChartResult",
    "ErrorResult",
    "JsonResult",
    "PgCopyTextResult",
    "TableResult",
    "TextResult",
    "ToolResult",
    "ToolResultBase",
]


class ToolResultBase(BaseModel, ABC):
    """Базовый класс для конкретных ToolResult-вариантов.

    Не для использования как тип значения — используй ToolResult (alias
    на discriminated union). Публичен только для наследования.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class TextResult(ToolResultBase):
    """Простой текст. Используется tools без структурированного payload'а."""

    kind: Literal["text"] = "text"
    text: str
    metadata: Mapping[str, str] = Field(default_factory=dict)


class JsonResult(ToolResultBase):
    """JSON-сериализуемый payload."""

    kind: Literal["json"] = "json"
    payload: Any
    metadata: Mapping[str, str] = Field(default_factory=dict)


class TableResult(ToolResultBase):
    """Табличный payload: список записей-строк (headers берутся из ключей)."""

    kind: Literal["table"] = "table"
    rows: Sequence[Mapping[str, Any]]
    note: str | None = None
    """Footer-контекст под таблицей: усечение, предупреждения и т.п."""
    metadata: Mapping[str, str] = Field(default_factory=dict)


class PgCopyTextResult(ToolResultBase):
    """Дамп COPY ... TO STDOUT (FORMAT TEXT, HEADER) (tab-delimited).

    LLM получает текст как есть. UI парсит через iter_rows (формат
    фиксирован Postgres: \\t-делимитер, \\n-разделитель, спецсимволы
    backslash-эскейпнуты, NULL = \\N) -> markdown-таблица.
    """

    kind: Literal["pg_copy_text"] = "pg_copy_text"
    text: str
    metadata: Mapping[str, str] = Field(default_factory=dict)

    _UNESCAPE: ClassVar[Mapping[str, str]] = {
        "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
        "\\": "\\",
    }

    def iter_rows(self) -> Iterator[list[str | None]]:
        """Yield строки (header первой) как list ячеек; NULL (\\N) -> None."""
        if not self.text:
            return
        lines = self.text.split("\n")
        if lines and lines[-1] == "":  # COPY терминирует каждую строку \n
            lines.pop()
        for line in lines:
            yield [self._unescape(field) for field in line.split("\t")]

    @classmethod
    def _unescape(cls, field: str) -> str | None:
        """Развернуть COPY TEXT-эскейпы одной ячейки; \\N -> None (NULL)."""
        if field == "\\N":
            return None
        if "\\" not in field:
            return field
        out: list[str] = []
        i, n = 0, len(field)
        while i < n:
            c = field[i]
            if c == "\\" and i + 1 < n:
                out.append(cls._UNESCAPE.get(field[i + 1], field[i + 1]))
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)


class ChartResult(ToolResultBase):
    """Интерактивный график: Plotly figure spec как чистый dict.

    spec — Plotly figure JSON ({"data": [...], "layout": {...}}). Домен не
    зависит от plotly: хранит и валидирует только структуру dict, рендер
    живёт в presentation-слое (chart_figure).
    """

    kind: Literal["chart"] = "chart"
    spec: Mapping[str, Any]
    title: str | None = None
    """Человекочитаемый заголовок графика — для сводки в LLM и подписи в UI."""
    metadata: Mapping[str, str] = Field(default_factory=dict)


class ErrorResult(ToolResultBase):
    """Tool не выполнен: ошибка домена, отклонение guard'а, невалидные args.

    Отдельный sealed-вариант, чтобы UI рендерил ошибки иначе (step.is_error).
    """

    kind: Literal["error"] = "error"
    message: str
    error_kind: str
    metadata: Mapping[str, str] = Field(default_factory=dict)


ToolResult: TypeAlias = Annotated[
    TextResult
    | JsonResult
    | TableResult
    | PgCopyTextResult
    | ChartResult
    | ErrorResult,
    Field(discriminator="kind"),
]
