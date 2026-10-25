"""Теги, которыми агент размечает текст для модели: единственное место их имён.

Служебный блок `system-reminder` отделяет вложения агента от слов
пользователя, `tool_use_error` помечает результат-ошибку, `truncated-output`
— пометку обрезки (план, разделы 5.12, 5.13). Тег пишется только отсюда:
AgentTag знает своё имя и собирает скобки, оборачивает текст строкой или
блоком с переводами строк и снимает блочную обёртку. Локальная команда
пользователя (`/compact`) записывается в разговор тегами вызова и вывода
(LocalCommandText), как у эталона (docs/prompt_assembly, texts/compact/
local_command_records_manual.txt). TagShield обезвреживает
теги в чужом тексте — словах пользователя, результатах инструментов и в
содержимом собственных обёрток: теги находит токенизатор стандартной
библиотеки `html.parser.HTMLParser`, найденное экранирует `html.escape`,
и модель не принимает чужой текст за разметку агента.

ReplyTag — теги, которыми модель по инструкции агента размечает свой ответ
(блоки разбора и пересказа при сжатии, раздел 5.16); их содержимое агент
извлекает и вырезает тем же токенизатором. Защита их не экранирует:
`<summary>` — обычный элемент HTML, который бывает в тексте пользователя и
результатах.
"""

from __future__ import annotations

import html
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from typing import ClassVar

from boba.core.commands import CommandCall, CommandMark

__all__ = ["AgentTag", "LocalCommandText", "ReplyTag", "TagShield"]


class AgentTag(StrEnum):
    """Имена тегов агента и операции над одним тегом."""

    SYSTEM_REMINDER = "system-reminder"
    TOOL_USE_ERROR = "tool_use_error"
    TRUNCATED_OUTPUT = "truncated-output"
    LOCAL_COMMAND_CAVEAT = "local-command-caveat"
    COMMAND_NAME = "command-name"
    COMMAND_MESSAGE = "command-message"
    COMMAND_ARGS = "command-args"
    LOCAL_COMMAND_STDOUT = "local-command-stdout"

    @property
    def open(self) -> str:
        return f"<{self.value}>"

    @property
    def close(self) -> str:
        return f"</{self.value}>"

    def wrap(self, text: str) -> str:
        """Текст в теге одной строкой: `<tag>text</tag>`. Теги агента внутри
        текста экранируются: содержимое часто чужое (сообщение пользователя,
        описание инструмента стороннего сервера) и не должно закрыть блок."""
        return f"{self.open}{TagShield(list(AgentTag)).shield(text)}{self.close}"

    def wrap_block(self, text: str) -> str:
        """Текст блоком: тег, перевод строки, текст, перевод строки, тег;
        содержимое экранируется так же, как в `wrap`."""
        inner = TagShield(list(AgentTag)).shield(text)

        return f"{self.open}\n{inner}\n{self.close}"

    def unwrap(self, text: str) -> str:
        """Текст без строчной обёртки `wrap`; текст без неё — как есть."""
        if text.startswith(self.open) and text.endswith(self.close):
            return text[len(self.open) : -len(self.close)]

        return text

    def unwrap_block(self, text: str) -> str:
        """Текст без блочной обёртки; текст без неё возвращается как есть."""
        head = f"{self.open}\n"
        tail = f"\n{self.close}"
        if text.startswith(head) and text.endswith(tail):
            return text[len(head) : -len(tail)]

        return text


class LocalCommandText:
    """Текст записи вызова локальной команды пользователя и его разбор.

    Вызов `/<имя> <аргументы>`, который агент исполнил сам, без модели,
    пишется в разговор записью с тегами command-name, command-message и
    command-args; так его видит модель в следующих запросах. Обратный
    разбор отдаёт вызов текстом, каким его набрал пользователь, — им вход
    ACP показывает запись клиенту. Держат цикл хода (TurnLoop) и вход ACP.
    """

    INDENT: ClassVar[str] = "\n            "

    def call(self, call: CommandCall) -> str:
        """Текст записи вызова call."""
        name = f"{CommandMark.PREFIX.value}{call.name}"
        parts = [
            AgentTag.COMMAND_NAME.wrap(name),
            AgentTag.COMMAND_MESSAGE.wrap(call.name),
            AgentTag.COMMAND_ARGS.wrap(call.rest),
        ]

        return self.INDENT.join(parts)

    def parsed(self, text: str) -> CommandCall | None:
        """Вызов из текста записи; None — текст не запись вызова."""
        if not text.startswith(AgentTag.COMMAND_NAME.open):
            return None

        name = self._inner(text, AgentTag.COMMAND_MESSAGE)
        if not name:
            return None

        rest = self._inner(text, AgentTag.COMMAND_ARGS)
        if rest is None:
            return None

        return CommandCall(name=name, rest=rest)

    @staticmethod
    def _inner(text: str, tag: AgentTag) -> str | None:
        start = text.find(tag.open)
        if start < 0:
            return None

        opened = start + len(tag.open)
        end = text.find(tag.close, opened)
        if end < 0:
            return None

        return text[opened:end]


class ReplyTag(StrEnum):
    """Имена тегов в ответе модели и извлечение их содержимого."""

    SUMMARY = "summary"
    ANALYSIS = "analysis"

    def extract(self, text: str) -> str | None:
        """Содержимое первого закрытого блока `<tag>…</tag>`; None — блока
        нет либо открытый тег не закрыт."""
        found = self._block(text)
        if found is None:
            return None

        opened, closed = found

        return text[opened.end : closed.start]

    def without(self, text: str) -> str:
        """Текст без первого закрытого блока `<tag>…</tag>` вместе с тегами;
        без блока — как есть."""
        found = self._block(text)
        if found is None:
            return text

        opened, closed = found

        return text[: opened.start] + text[closed.end :]

    def _block(self, text: str) -> tuple[TagSpan, TagSpan] | None:
        """Открывающий и закрывающий теги первого закрытого блока."""
        spans = TagScanner(frozenset({self.value})).scan(text)
        opened: TagSpan | None = None
        for span in spans:
            if not span.closing:
                if opened is None:
                    opened = span

                continue

            if opened is not None:
                return opened, span

        return None


@dataclass(frozen=True)
class TagSpan:
    """Где в тексте стоит тег: границы в знаках и закрывающий ли он."""

    start: int
    end: int
    closing: bool


class TagScanner(HTMLParser):
    """Токенизатор тегов поверх HTMLParser: отдаёт границы названных тегов.

    Разбор делает стандартная библиотека: регистр имени, атрибуты,
    пробелы и самозакрывающаяся форма — её забота. Режим CDATA для
    `script` и `style` отключён, иначе `<script>` в чужом тексте спрятал бы
    следующий за ним тег агента. Создают TagShield и ReplyTag на каждый
    разбор.
    """

    def __init__(self, names: frozenset[str]) -> None:
        super().__init__(convert_charrefs=False)
        self._names = names
        self._text = ""
        self._line_starts: list[int] = [0]
        self.spans: list[TagSpan] = []

    def scan(self, text: str) -> Sequence[TagSpan]:
        self.reset()
        self._text = text
        self._line_starts = [0]
        for index, char in enumerate(text):
            if char == "\n":
                self._line_starts.append(index + 1)

        self.spans = []
        self.feed(text)
        self.close()

        return self.spans

    def set_cdata_mode(self, elem: str, *, escapable: bool = False) -> None:
        """Режим CDATA не включается: `<script>` в чужом тексте не должен
        спрятать идущий за ним тег агента."""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._take_start(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._take_start(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._names:
            return

        start = self._index()
        end = self._text.find(">", start)
        if end < 0:
            end = len(self._text) - 1

        self.spans.append(TagSpan(start=start, end=end + 1, closing=True))

    def _take_start(self, tag: str) -> None:
        if tag not in self._names:
            return

        raw = self.get_starttag_text()
        if raw is None:
            return

        start = self._index()
        self.spans.append(TagSpan(start=start, end=start + len(raw), closing=False))

    def _index(self) -> int:
        """Позиция текущего тега в тексте по строке и смещению парсера."""
        lineno, offset = self.getpos()

        return self._line_starts[lineno - 1] + offset


class TagShield:
    """Защита чужого текста от подделки тегов агента.

    Создаёт сборщик запроса с теми тегами, которым модель верит: служебный
    блок и признак ошибки; обёртки AgentTag берут защиту от всех тегов для
    своего содержимого. Каждый найденный тег в любом регистре экранируется
    `html.escape`. Двойники из других алфавитов и невидимые символы внутри
    имени защита не ловит — это остаточный риск.
    """

    def __init__(self, tags: Sequence[AgentTag]) -> None:
        self._scanner = TagScanner(frozenset(tag.value for tag in tags))

    def shield(self, text: str) -> str:
        spans = self._scanner.scan(text)
        if not spans:
            return text

        return "".join(self._pieces(text, spans))

    @staticmethod
    def _pieces(text: str, spans: Sequence[TagSpan]) -> Iterator[str]:
        cursor = 0
        for span in spans:
            yield text[cursor : span.start]
            yield html.escape(text[span.start : span.end], quote=False)
            cursor = span.end

        yield text[cursor:]
