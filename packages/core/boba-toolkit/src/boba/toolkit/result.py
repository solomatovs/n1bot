"""Sealed-семейство ToolResult (kind); pack_result -> (content, artifact)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
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
    "MultiResult",
    "Produces",
    "ResultTooLargeError",
    "ShellResult",
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

    elapsed_ms: int = 0
    """Время вызова целиком, его проставляет обвязка запуска; 0 — не измерено.

    Лежит в результате, а не в шаге ленты: артефакт переживает перезагрузку
    вкладки, и сборка ленты из истории показывает то же время, что и live.
    У ShellResult рядом живёт duration_ms — время самой команды без запуска
    песочницы.
    """

    @abstractmethod
    def llm_text(self) -> str:
        """Текст результата для LLM: он же content конверта вызова."""


class TextResult(ToolResultBase):
    """Простой текст. Используется tools без структурированного payload'а."""

    kind: Literal["text"] = "text"
    text: str
    language: str = ""
    """Язык markdown-блока показа: зеркало ScriptCall.lang для вызова.

    Пусто — текст уже markdown и рисуется как есть; заданный язык уводит
    его в блок (дамп csv, лог, вывод чужого формата).
    """
    note: str | None = None
    """Footer-контекст под текстом: источник, окно строк, усечение."""
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        if self.note is None:
            return self.text

        if not self.text.strip():
            return self.note

        return f"{self.text}\n\n{self.note}"


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


class ShellResult(ToolResultBase):
    """Итог shell-команды: код возврата и её потоки вывода.

    Сама команда здесь не хранится: её рисует вход шага из аргументов
    вызова. Потоки хранятся врозь: LLM разбирает их по отдельности, а
    пользователю показывается один — stdout, а когда он пуст, stderr.
    """

    kind: Literal["shell"] = "shell"
    exit_code: int
    stdout: str
    stdout_bytes: int
    """Полный размер stdout до усечения."""
    stdout_truncated: bool
    stderr: str
    stderr_bytes: int
    """Полный размер stderr до усечения."""
    stderr_truncated: bool
    duration_ms: int
    timed_out: bool
    diagnostic: str
    metadata: Mapping[str, str] = Field(default_factory=dict)

    LLM_SKIP: ClassVar[frozenset[str]] = frozenset({"kind", "ok", "metadata"})
    """Поля конверта: в отчёте для LLM им места нет."""

    @property
    def shows_stdout(self) -> bool:
        """Показывается stdout; пустой stdout уступает место stderr."""
        return bool(self.stdout.strip())

    @property
    def output(self) -> str:
        """Поток, который видит пользователь."""
        if self.shows_stdout:
            return self.stdout

        return self.stderr

    @property
    def truncated(self) -> bool:
        """Усечён ли показанный поток."""
        if self.shows_stdout:
            return self.stdout_truncated

        return self.stderr_truncated

    def llm_text(self) -> str:
        report = self.model_dump(exclude=set(self.LLM_SKIP))

        return json.dumps(report, ensure_ascii=False)


class MultiResult(ToolResultBase):
    """Несколько итогов одного вызова по порядку: команды одного запроса.

    Составляется из тех же вариантов семейства — выборка остаётся таблицей,
    DML счётчиком. Набор целен: команды идут одной транзакцией, поэтому
    частично провалившегося набора не бывает.
    """

    kind: Literal["multi"] = "multi"
    items: Sequence[ToolResult]
    metadata: Mapping[str, str] = Field(default_factory=dict)

    def llm_text(self) -> str:
        parts: list[str] = []

        for index, item in enumerate(self.items, start=1):
            parts.append(f"[{index}] {item.llm_text()}")

        return "\n\n".join(parts)


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
    | AffectedSqlResult
    | ChartResult
    | CustomElementResult
    | DiagramResult
    | ShellResult
    | MultiResult
    | ErrorResult,
    Field(discriminator="kind"),
]

MultiResult.model_rebuild()
"""Набор ссылается на семейство целиком: ссылка разрешается после него."""


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


class Produces(BaseModel):
    """Объявление у инструмента, какие виды результата он отдаёт: метаданные
    Annotated возвращаемого типа; каталог workflow показывает их у порта result."""

    model_config = ConfigDict(frozen=True)

    kinds: tuple[str, ...]

    @classmethod
    def of(cls, *results: type[ToolResultBase]) -> Produces:
        kinds: list[str] = []
        for result in results:
            kinds.append(str(result.model_fields["kind"].default))

        return cls(kinds=tuple(kinds))
