"""Sealed-семейство ToolResult (kind); pack_result -> (content, artifact)."""

from __future__ import annotations

import json
from abc import ABC
from collections.abc import Iterator, Mapping, Sequence
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    TypeAlias,
    assert_never,
    cast,
    get_args,
)

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    "ChartResult",
    "ErrorResult",
    "JsonResult",
    "PgCopyTextResult",
    "TableResult",
    "TextResult",
    "ToolArtifact",
    "ToolResult",
    "ToolResultBase",
    "pack_result", "render_for_llm",
]


class ToolResultBase(BaseModel, ABC):
    """База вариантов (тип значения — ToolResult); ok — единственный признак успеха:
    ненулевой код выхода или ошибка сервера обязаны выставить ok=False."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool = True


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
    """Дамп COPY ... TO STDOUT (FORMAT TEXT, HEADER), tab-delimited."""

    kind: Literal["pg_copy_text"] = "pg_copy_text"
    text: str
    metadata: Mapping[str, str] = Field(default_factory=dict)

    _UNESCAPE: ClassVar[Mapping[str, str]] = {
        "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
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


class ChartResult(ToolResultBase):
    """Интерактивный график: Plotly figure spec как чистый dict."""

    kind: Literal["chart"] = "chart"
    spec: Mapping[str, Any]
    title: str | None = None
    """Человекочитаемый заголовок графика — для сводки в LLM и подписи в UI."""
    metadata: Mapping[str, str] = Field(default_factory=dict)


class ErrorResult(ToolResultBase):
    """Tool не выполнен; UI рендерит такой результат как ошибку."""

    kind: Literal["error"] = "error"
    ok: bool = False
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


def render_for_llm(result: ToolResult) -> str:
    content: str
    match result:
        case TextResult(text=t):
            content = t
        case JsonResult(payload=p):
            content = json.dumps(p, ensure_ascii=False)
        case TableResult(rows=r, note=n):
            body = json.dumps(r, ensure_ascii=False)
            content = body if n is None else f"{body}\n\n{n}"
        case PgCopyTextResult(text=t):
            content = t
        case ChartResult(title=title):
            content = f"[chart rendered: {title}]" if title else "[chart rendered]"
        case ErrorResult(message=m):
            content = m
        case _ as never:
            assert_never(never)
    return content


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
        if isinstance(artifact, ToolResultBase):
            return cast(ToolResult, artifact)
        if isinstance(artifact, Mapping) and artifact.get("kind") in cls._KINDS:
            return cls._ADAPTER.validate_python(dict(artifact))
        return None
