"""Sealed-семейство ToolResult (kind); pack_result -> (content, artifact)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    TypeAlias,
    get_args,
)

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    "AffectedSqlResult",
    "ChartResult",
    "CustomElementResult",
    "DiagramResult",
    "ErrorResult",
    "JsonResult",
    "PgCopyTextResult",
    "ResultTooLargeError",
    "TableResult",
    "TextResult",
    "ToolArtifact",
    "ToolResult",
    "ToolResultBase",
    "pack_result",
    "render_for_llm",
]


class ResultTooLargeError(Exception):
    """Выдача больше потолка; сообщение готово для пользователя и LLM."""

    @classmethod
    def bytes_limit(cls, max_bytes: int) -> ResultTooLargeError:
        return cls(f"result exceeded {max_bytes} bytes; add LIMIT to the query")

    @classmethod
    def chars_limit(cls, max_chars: int) -> ResultTooLargeError:
        return cls(f"page content exceeded {max_chars} characters")


class ToolResultBase(BaseModel, ABC):
    """База вариантов (тип значения — ToolResult); ok — единственный признак успеха:
    ненулевой код выхода или ошибка сервера обязаны выставить ok=False."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool = True

    @abstractmethod
    def llm_text(self) -> str:
        """Текст результата для LLM: он же content конверта вызова."""


class TextResult(ToolResultBase):
    """Простой текст. Используется tools без структурированного payload'а."""

    kind: Literal["text"] = "text"
    text: str
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        return self.text


class JsonResult(ToolResultBase):
    """JSON-сериализуемый payload."""

    kind: Literal["json"] = "json"
    payload: Any
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False)


class TableResult(ToolResultBase):
    """Табличный payload: список записей-строк (headers берутся из ключей)."""

    kind: Literal["table"] = "table"
    rows: Sequence[Mapping[str, Any]]
    note: str | None = None
    """Footer-контекст под таблицей: усечение, предупреждения и т.п."""
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        body = json.dumps(self.rows, ensure_ascii=False)
        if self.note is None:
            return body

        return f"{body}\n\n{self.note}"


class PgCopyTextResult(ToolResultBase):
    """Дамп COPY ... TO STDOUT (FORMAT TEXT, HEADER), tab-delimited."""

    kind: Literal["pg_copy_text"] = "pg_copy_text"
    text: str
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        return self.text

    _UNESCAPE: ClassVar[Mapping[str, str]] = {
        "n": "\n",
        "t": "\t",
        "r": "\r",
        "b": "\b",
        "f": "\f",
        "v": "\v",
        "\\": "\\",
    }

    def iter_rows(self) -> Iterator[list[str | None]]:
        if not self.text:
            return
        lines = self.text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        for line in lines:
            yield [self._unescape(field) for field in line.split("\t")]

    @classmethod
    def _unescape(cls, field: str) -> str | None:
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


class AffectedSqlResult(ToolResultBase):
    """Запрос без выборки: DML/DDL отработал, строк нет — только счётчик."""

    kind: Literal["affected"] = "affected"
    affected_rows: int | None
    """Число затронутых строк; None там, где драйвер счётчика не даёт (DDL)."""
    status: str | None
    """Нативный статус выполнения, напр. statusmessage psycopg — 'DELETE 5'."""
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        if self.status:
            return self.status

        if self.affected_rows is not None:
            return f"affected rows: {self.affected_rows}"

        return "statement executed"


class ChartResult(ToolResultBase):
    """Интерактивный график: Plotly figure spec как чистый dict."""

    kind: Literal["chart"] = "chart"
    spec: Mapping[str, Any]
    title: str | None = None
    """Человекочитаемый заголовок графика — для сводки в LLM и подписи в UI."""
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        if self.title:
            return f"[chart rendered: {self.title}]"

        return "[chart rendered]"


class CustomElementResult(ToolResultBase):
    """Кастомный UI-элемент: имя jsx-компонента и его props."""

    kind: Literal["custom_element"] = "custom_element"
    element: str
    """Имя компонента: файл public/elements/<element>.jsx."""
    props: Mapping[str, Any]
    title: str | None = None
    """Человекочитаемый заголовок — для сводки в LLM и подписи в UI."""
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        if self.title:
            return f"[{self.element} rendered: {self.title}]"

        return f"[{self.element} rendered]"


class DiagramResult(ToolResultBase):
    """Диаграмма: спека mermaid и путь её файла в workspace треда."""

    kind: Literal["diagram"] = "diagram"
    spec: str
    path: str
    title: str | None = None
    """Человекочитаемый заголовок — для сводки в LLM и подписи в UI."""
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        if self.title:
            return f"[diagram rendered: {self.title} ({self.path})]"

        return f"[diagram rendered: {self.path}]"


class ErrorResult(ToolResultBase):
    """Tool не выполнен; UI рендерит такой результат как ошибку."""

    kind: Literal["error"] = "error"
    ok: bool = False
    message: str
    error_kind: str
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        return self.message


ToolResult: TypeAlias = Annotated[
    TextResult
    | JsonResult
    | TableResult
    | PgCopyTextResult
    | AffectedSqlResult
    | ChartResult
    | CustomElementResult
    | DiagramResult
    | ErrorResult,
    Field(discriminator="kind"),
]


def render_for_llm(result: ToolResultBase) -> str:
    """Текст результата для LLM; форма варианта здесь не нужна."""
    return result.llm_text()


def pack_result(result: ToolResult) -> tuple[str, ToolResult]:
    return render_for_llm(result), result


class ToolArtifact:
    """Поднимает artifact в модель: langgraph сериализует его в обычный dict."""

    _ADAPTER: ClassVar[TypeAdapter[ToolResult]] = TypeAdapter(ToolResult)
    _KINDS: ClassVar[frozenset[str]] = frozenset(
        get_args(variant.model_fields["kind"].annotation)[0]
        for variant in get_args(get_args(ToolResult)[0])
    )

    @classmethod
    def revive(cls, artifact: Any) -> ToolResult | None:
        """Артефакт -> вариант семейства; чужое значение — None.

        Готовая модель проходит тем же адаптером: у дискриминированного
        союза он узнаёт вариант по kind и возвращает сам объект, поэтому
        приведения типа тут не нужно.
        """
        if isinstance(artifact, ToolResultBase):
            return cls._ADAPTER.validate_python(artifact)

        if not isinstance(artifact, Mapping):
            return None

        if artifact.get("kind") not in cls._KINDS:
            return None

        return cls._ADAPTER.validate_python(dict(artifact))
