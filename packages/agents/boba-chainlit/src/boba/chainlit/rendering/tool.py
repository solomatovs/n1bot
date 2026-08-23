"""Представление вызова инструмента и его результата в ленте.

Три слоя, общих для обоих семейств: markdown-примитивы (Fence, JsonBlock,
FieldLines, NoteLine) — один набор кирпичей; формы показа (Rendering) —
что лента умеет отрисовать; рендеры — ToolCallMarkdown для входа шага по
объявлению ToolCallView, ToolResultView/ToolResultMarkdown для результата.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, assert_never

from tabulate import tabulate

import chainlit as cl
from boba.toolkit.calls import HiddenCall, JsonCall, ScriptCall, ToolCallView
from boba.toolkit.result import (
    AffectedSqlResult,
    ChartResult,
    CustomElementResult,
    DiagramResult,
    ErrorResult,
    JsonResult,
    MultiResult,
    ShellResult,
    TableResult,
    TextResult,
    ToolResult,
)
from chainlit.element import ElementDisplay

__all__ = [
    "ChartRendering",
    "CustomElementRendering",
    "DiagramRendering",
    "Fence",
    "FieldLines",
    "JsonBlock",
    "MarkdownRendering",
    "NoteLine",
    "ToolCallMarkdown",
    "ToolResultMarkdown",
    "ToolResultRendering",
    "ToolResultView",
]


class Fence:
    """Ограда markdown-блока: одна на все места, где текст едет блоком."""

    BASE: ClassVar[str] = "```"

    @classmethod
    def around(cls, text: str, lang: str = "") -> str:
        """Блок с оградой длиннее любой внутри текста: она его не разорвёт."""
        fence = cls.BASE
        while fence in text:
            fence += "`"

        return f"{fence}{lang}\n{text}\n{fence}"


class JsonBlock:
    """Json-показ: один на оба семейства (payload результата и args вызова)."""

    @staticmethod
    def pretty(payload: Any) -> str:
        """Текст для показа с подсветкой json; чужие типы приводятся строкой:
        аргументы вызова приходят из протокола и сериализуемость не обещают."""
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def block(payload: Any) -> str:
        """Markdown-блок; payload результата обязан быть json-сериализуем."""
        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        if "\n" not in pretty:
            return f"`{pretty}`"

        return f"\n```json\n{pretty}\n```\n"


class FieldLines:
    """Строки именованных значений: **имя:** `значение`, многострочное — блоком."""

    @classmethod
    def render(cls, fields: Mapping[str, Any]) -> Iterator[str]:
        for name, value in fields.items():
            if isinstance(value, str) and "\n" in value:
                yield f"**{name}:**\n{Fence.around(value)}"
                continue

            if isinstance(value, str):
                yield f"**{name}:** `{value}`"
                continue

            rendered = json.dumps(value, ensure_ascii=False, default=str)
            yield f"**{name}:** `{rendered}`"


class NoteLine:
    """Служебная строка курсивом: статус, усечение, подпись вместо элемента."""

    @staticmethod
    def render(text: str) -> str:
        return f"_{text}_"


@dataclass(frozen=True)
class MarkdownRendering:
    """Содержимое показывается текстом.

    language — подсветка chainlit для сырого текста (json-вход); None —
    текст уже markdown и рендерится как есть.
    """

    markdown: str
    language: str | None = None

    @property
    def show_input(self) -> str | bool:
        """Значение step.show_input для входа шага chainlit."""
        if self.language:
            return self.language

        return True


@dataclass(frozen=True)
class ChartRendering:
    """Результат показывается интерактивным Plotly-графиком."""

    spec: Mapping[str, Any]
    title: str | None

    def chat_element(self, *, display: ElementDisplay = "inline") -> cl.Plotly:
        """cl.Plotly из spec — единственное место, знающее про plotly."""
        from plotly import graph_objects as go  # noqa: PLC0415

        figure = go.Figure(dict(self.spec))
        return cl.Plotly(name=self.title or "chart", figure=figure, display=display)


@dataclass(frozen=True)
class CustomElementRendering:
    """Результат показывается jsx-компонентом public/elements/<element>.jsx."""

    element: str
    props: Mapping[str, Any]
    title: str | None

    def chat_element(self, *, display: ElementDisplay = "inline") -> cl.CustomElement:
        return cl.CustomElement(
            name=self.element, props=dict(self.props), display=display
        )


@dataclass(frozen=True)
class DiagramRendering:
    """Результат показывается отрисованной диаграммой mermaid.

    В ленте — компактная карточка того же CanvasView, что рисует панель;
    клик по ней показывает файл в канвасе.
    """

    ELEMENT: ClassVar[str] = "CanvasView"

    spec: str
    path: str
    title: str | None

    def chat_element(self, *, display: ElementDisplay = "inline") -> cl.CustomElement:
        label = self.title
        if not label:
            label = self.path

        props = {
            "kind": "mermaid",
            "path": self.path,
            "label": label,
            "text": self.spec,
            "preview": True,
        }
        return cl.CustomElement(name=self.ELEMENT, props=props, display=display)


ToolResultRendering = (
    MarkdownRendering | ChartRendering | CustomElementRendering | DiagramRendering
)


class ToolCallMarkdown:
    """Вход шага: объявление инструмента (ToolCallView) + аргументы вызова.

    Зеркало ToolResultMarkdown: результат рендерится из значения, вход —
    из правила и аргументов протокола LLM. None — вход не показывается.
    """

    def __init__(self, view: ToolCallView, args: Mapping[str, Any]) -> None:
        self._view = view
        self._args = args

    def render(self) -> MarkdownRendering | None:
        match self._view:
            case HiddenCall():
                return None
            case ScriptCall() as script:
                return self._script(script)
            case JsonCall():
                return self._json()
            case _ as never:
                assert_never(never)

    def _json(self) -> MarkdownRendering:
        return MarkdownRendering(JsonBlock.pretty(dict(self._args)), language="json")

    def _script(self, script: ScriptCall) -> MarkdownRendering:
        """Скрипт блоком с языком, прочие аргументы следом; пустые опускаются."""
        code = self._args.get(script.arg)
        if not isinstance(code, str):
            # объявление разошлось со схемой: показываем как есть, не гадая
            return self._json()

        blocks = [Fence.around(code.strip("\n"), script.lang)]

        rest = {key: value for key, value in self._args.items() if key != script.arg}
        for name, value in rest.items():
            if isinstance(value, str) and not value:
                continue

            blocks.extend(FieldLines.render({name: value}))

        return MarkdownRendering("\n\n".join(blocks))


class ToolResultView:
    """Выбирает форму представления для одного ToolResult."""

    def __init__(self, result: ToolResult) -> None:
        self._result = result

    def render(self) -> ToolResultRendering:
        match self._result:
            case ChartResult(spec=spec, title=title):
                return ChartRendering(spec=spec, title=title)
            case CustomElementResult(element=element, props=props, title=title):
                return CustomElementRendering(element=element, props=props, title=title)
            case DiagramResult(spec=spec, path=path, title=title):
                return DiagramRendering(spec=spec, path=path, title=title)
            case (
                TextResult()
                | JsonResult()
                | TableResult()
                | AffectedSqlResult()
                | ShellResult()
                | MultiResult()
                | ErrorResult()
            ):
                return MarkdownRendering(ToolResultMarkdown(self._result).render())
            case _ as never:
                assert_never(never)


class ToolResultMarkdown:
    """Оборачивает один ToolResult и рендерит его в markdown для Chainlit."""

    def __init__(self, result: ToolResult) -> None:
        self._result = result

    def render(self) -> str:  # noqa: PLR0911
        match self._result:
            case TextResult() as text:
                return self._text_block(text)
            case JsonResult(payload=p):
                return JsonBlock.block(p)
            case TableResult(rows=rows, note=note):
                return self._table_block(rows, note)
            case AffectedSqlResult(affected_rows=n, status=s):
                return self._affected_block(n, s)
            case ShellResult() as shell:
                return self._shell_block(shell)
            case MultiResult(items=items):
                return self._multi_block(items)
            case ChartResult() | CustomElementResult() | DiagramResult() as visual:
                return self._visual_block(visual)
            case ErrorResult(message=m):
                return self._error_block(m)
            case _ as never:
                assert_never(never)

    @staticmethod
    def _text_block(result: TextResult) -> str:
        """Текст блоком с языком либо как есть, под ним подпись источника.

        Пустой текст блока не получает: от пустой страницы и грепа без
        совпадений остаётся одна подпись.
        """
        if not result.text.strip():
            if result.note is None:
                return ""

            return NoteLine.render(result.note)

        body = result.text
        if result.language:
            body = Fence.around(result.text.strip("\n"), result.language)

        if result.note is None:
            return body

        return f"{body}\n\n{NoteLine.render(result.note)}"

    @staticmethod
    def _visual_block(
        result: ChartResult | CustomElementResult | DiagramResult,
    ) -> str:
        """Подпись вместо самого элемента: элемент уходит своим rendering."""
        match result:
            case ChartResult(title=title):
                if title:
                    return NoteLine.render(f"(chart: {title})")
                return NoteLine.render("(chart)")
            case CustomElementResult(title=title):
                if title:
                    return NoteLine.render(f"(element: {title})")
                return NoteLine.render("(element)")
            case DiagramResult(path=path, title=title):
                if title:
                    return NoteLine.render(f"(diagram: {title})")
                return NoteLine.render(f"(diagram: {path})")
            case _ as never:
                assert_never(never)

    @classmethod
    def _multi_block(cls, items: Sequence[ToolResult]) -> str:
        """Итоги команд по порядку, каждый со своим номером.

        Вариант набора рисуется теми же рендерами, что и одиночный итог:
        выборка остаётся таблицей, DML — строкой счётчика.
        """
        blocks: list[str] = []

        for index, item in enumerate(items, start=1):
            blocks.append(NoteLine.render(f"statement {index}"))
            # блоки вариантов приходят со своими отбивками: склейка их ровняет
            blocks.append(cls(item).render().strip("\n"))

        return "\n\n".join(blocks)

    @classmethod
    def _shell_block(cls, result: ShellResult) -> str:
        """Вывод команды блоком, под ним код возврата обычной строкой.

        Сама команда во вход шага попадает через ScriptCall — здесь только
        итог её выполнения. Шапка блока называет показанный поток: chainlit
        берёт её из ограды регуляркой language-(\\w+), человеческий текст
        туда не проходит — поэтому код возврата живёт строкой под блоком.
        """
        blocks: list[str] = []

        output = result.output.strip("\n")
        if output:
            heading = "stdout"
            if not result.shows_stdout:
                heading = "stderr"
            blocks.append(Fence.around(output, heading))
        else:
            blocks.append(NoteLine.render("(no output)"))

        blocks.append(cls._shell_note(result))

        return "\n\n".join(blocks)

    @staticmethod
    def _shell_note(result: ShellResult) -> str:
        """Итог выполнения одной строкой: код возврата всегда, помехи следом.

        Отрицательный код возврата — процесс убит сигналом: так его отдаёт
        python, и «exit code: -9» читался бы как ошибка рендера.
        """
        notes: list[str] = []

        if result.exit_code < 0:
            notes.append(f"killed by signal {-result.exit_code}")
        else:
            notes.append(f"exit code: {result.exit_code}")

        if result.timed_out:
            notes.append("timed out")

        if result.truncated:
            notes.append("output truncated")

        if result.diagnostic:
            notes.append(result.diagnostic)

        return NoteLine.render("; ".join(notes))

    @staticmethod
    def _error_block(message: str) -> str:
        if "\n" in message:
            return f"**Error:**\n\n{message}"

        return f"**Error:** {message}"

    @staticmethod
    def _affected_block(affected_rows: int | None, status: str | None) -> str:
        if affected_rows is None:
            if status is None:
                return NoteLine.render("query executed")
            return NoteLine.render(status)

        counted = f"rows affected: {affected_rows}"
        if status is None:
            return NoteLine.render(counted)
        return NoteLine.render(f"{counted} ({status})")

    def _table_block(
        self,
        rows: Sequence[Mapping[str, Any]],
        note: str | None,
    ) -> str:
        body = self._render_rows(rows)
        if note:
            return f"\n{body}\n\n{NoteLine.render(note)}"
        return f"\n{body}"

    @staticmethod
    def _flatten_cell(cell: str | None) -> str:
        if cell is None:
            return ""
        return cell.replace("\r\n", " ⏎ ").replace("\n", " ⏎ ").replace("\r", " ⏎ ")

    @classmethod
    def _cell(cls, value: Any) -> str:
        if value is None or isinstance(value, str):
            return cls._flatten_cell(value)
        if isinstance(value, (list, tuple, dict)):
            return cls._flatten_cell(json.dumps(value, ensure_ascii=False))
        return cls._flatten_cell(str(value))

    def _render_rows(self, rows: Sequence[Mapping[str, Any]]) -> str:
        if not rows:
            return NoteLine.render("(no rows)")

        flat = [{k: self._cell(v) for k, v in row.items()} for row in rows]
        return tabulate(
            flat,
            headers="keys",
            tablefmt="github",
            disable_numparse=True,
        )
