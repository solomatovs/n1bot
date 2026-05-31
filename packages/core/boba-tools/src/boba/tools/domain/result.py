"""ToolResult sealed-семейство: TextResult | JsonResult | ErrorResult.

`ToolResult` экспортируется как type alias на discriminated union
(`Annotated[TextResult | JsonResult | ErrorResult, Field(discriminator="kind")]`).
Это даёт строгую типизацию Pydantic-полей и нативный JSON-Schema `oneOf`.

Открытость для нового варианта: добавить класс `ToolResultBase`-подкласс,
прописать `kind: Literal["..."]`, расширить union в `ToolResult`. Все
существующие потребители используют `match`-выражения с discriminator —
pyright принудит дописать ветку.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Iterator, Mapping, Sequence
from typing import Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ErrorResult",
    "JsonResult",
    "PgCopyTextResult",
    "TableResult",
    "TextResult",
    "ToolResult",
    "ToolResultBase",
]


class ToolResultBase(BaseModel, ABC):
    """Базовый Pydantic-класс для наследования конкретных ToolResult-вариантов.

    Не для использования как тип значения — используй `ToolResult` (alias
    на discriminated union). Этот класс публичен только для наследования
    при добавлении нового варианта в sealed-семейство.
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
    """Табличный payload: список записей-строк (`headers` берутся из ключей).

    Tool декларирует «отрисуй меня таблицей»; UI рендерит markdown-таблицу,
    LLM получает тот же payload как JSON (структура важнее форматирования).
    """

    kind: Literal["table"] = "table"
    rows: Sequence[Mapping[str, Any]]
    note: str | None = None
    """Footer-контекст под таблицей: усечение, предупреждения и т.п."""
    metadata: Mapping[str, str] = Field(default_factory=dict)


class PgCopyTextResult(ToolResultBase):
    """Дамп `COPY ... TO STDOUT (FORMAT TEXT, HEADER)` (tab-delimited).

    Executor копит COPY-поток без парсинга ячеек. LLM получает текст как
    есть. UI парсит через `iter_rows` (формат фиксирован Postgres: `\\t`-
    делимитер, `\\n`-разделитель строк, спецсимволы backslash-эскейпнуты,
    NULL = `\\N`) → markdown-таблица. Первая строка — header.
    """

    kind: Literal["pg_copy_text"] = "pg_copy_text"
    text: str
    metadata: Mapping[str, str] = Field(default_factory=dict)

    _UNESCAPE: ClassVar[Mapping[str, str]] = {
        "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
        "\\": "\\",
    }

    def iter_rows(self) -> Iterator[list[str | None]]:
        """Yield строки (header первой) как list ячеек; NULL (`\\N`) → None."""
        if not self.text:
            return
        lines = self.text.split("\n")
        if lines and lines[-1] == "":  # COPY терминирует каждую строку \n
            lines.pop()
        for line in lines:
            yield [self._unescape(field) for field in line.split("\t")]

    @classmethod
    def _unescape(cls, field: str) -> str | None:
        """Развернуть COPY TEXT-эскейпы одной ячейки; `\\N` → None (NULL)."""
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


class ErrorResult(ToolResultBase):
    """Tool не выполнен: ошибка домена, отклонение guard'а, невалидные args.

    Отдельный sealed-вариант, чтобы провайдер мог рендерить ошибки иначе
    (например, для Anthropic — отдельный `is_error: true` блок).
    """

    kind: Literal["error"] = "error"
    message: str
    error_kind: str
    metadata: Mapping[str, str] = Field(default_factory=dict)


ToolResult: TypeAlias = Annotated[
    TextResult | JsonResult | TableResult | PgCopyTextResult | ErrorResult,
    Field(discriminator="kind"),
]
"""
Тип значения tool-результата: discriminated union по полю `kind`.

Используй для типизации полей моделей, возвращаемых значений функций и
параметров. Pydantic генерирует `oneOf` JSON Schema из коробки.
"""
