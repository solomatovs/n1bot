"""Value-объекты, которые носятся внутри агентских событий.

Принцип self-sufficient: если событие говорит «tool вернул результат» —
оно несёт ``LLMToolCall`` исходного вызова и :class:`ToolCallResult`;
если «tool упал» — несёт вызов и :class:`ToolCallFailure`. Sink, видящий
событие в изоляции, может полностью его отрисовать без обращения к
``MessageService`` или к буферам middleware.

Сам ``LLMToolCall`` (id + name + arguments-as-string) живёт в
:mod:`boba.domain.llm.models` — там же, где используется
:class:`LLMMessage`. Здесь — только payload'ы, специфичные для агентских
событий и не имеющие смысла на LLM-уровне.

История диалога (``MessageService``) реконструируется из суммы
``ContentSnapshot``-событий — каждое сообщение, попадающее в
:class:`MessageService`, имеет парный снапшот-event. Это означает, что
LLM-реквест в события не пакуется: предыдущие сообщения уже были
объявлены своими собственными снапшотами.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCallResult:
    """Результат успешного выполнения tool.

    Парный к :class:`ToolCallFailure` — оба представляют завершение
    одного и того же ``LLMToolCall``, разницу несёт сам тип.
    """

    content: str


@dataclass(frozen=True)
class ToolCallFailure:
    """Tool бросил :class:`ToolExecutionError` или невалидный JSON в args.

    ``error_kind`` — имя класса исходного исключения (для группировки
    в журнале/телеметрии). ``message`` — человеко-читаемое описание;
    то же сообщение записывается в ``MessageService`` как
    ``role="tool"`` — для LLM, а событие — для sink'ов.
    """

    error_kind: str
    message: str


@dataclass(frozen=True)
class ToolCallFormatFailure:
    """LLM нарушила формат content-as-JSON tool call'а.

    Эмитится до того, как удалось извлечь ``id``/``name`` (парсер
    провалился). Поэтому несём не ``LLMToolCall``, а сырой ``content``,
    который LLM выдала, плюс описание провала.

    ``raw_content`` — полная строка от модели; sink может её показать
    разработчику для диагностики промпта.
    """

    raw_content: str
    error_kind: str
    message: str
