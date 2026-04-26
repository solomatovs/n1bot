"""Value-объекты, которые носятся внутри агентских событий.

Принцип self-sufficient: каждое событие несёт всё нужное для отрисовки
sink'ом без обращения к MessageService или middleware-буферам.

LLMToolCall живёт в models (id + name + arguments). Здесь — только
payload'ы, специфичные для агент-событий.

История диалога (MessageService) реконструируется из суммы
ContentSnapshot-событий: каждое сообщение имеет парный снапшот-event,
поэтому LLM-реквест в события не пакуется.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class ToolCallResult:
    """Результат успешного выполнения tool.

    Парный к ToolCallFailure — оба представляют завершение
    одного и того же LLMToolCall, разницу несёт сам тип.
    """

    content: str


@dataclass(frozen=True)
class ToolCallFailure:
    """Tool бросил ToolExecutionError или невалидный JSON в args.

    error_kind — имя класса исходного исключения (для группировки
    в журнале/телеметрии). message — человеко-читаемое описание;
    то же сообщение записывается в MessageService как
    role="tool" — для LLM, а событие — для sink'ов.
    """

    error_kind: str
    message: str


# ═════════════════════════════════════════════════════════════════════
#  Feedback к LLM — discriminated union
# ═════════════════════════════════════════════════════════════════════
#
# to_llm_feedback возвращает один из этих
# вариантов; AgentErrorRouter диспетчирует их в узкие методы
# DialogueWriter. LLMMessage наружу не утекает — writer
# собирает его сам из примитивов.


@dataclass(frozen=True)
class LLMCritique:
    """Общая критика к LLM (role="user").

    Не привязана к конкретному tool_call'у. Используется для feedback'а,
    отражающего нарушение протокола / формата ответа в целом.
    """

    content: str


@dataclass(frozen=True)
class ToolCallRejection:
    """Подавление tool_call'а: ответ в слот конкретного вызова
    (role="tool" + tool_call_id).

    Используется guard'ами, которые **подавили** реальный запуск tool'а,
    но обязаны заполнить слот объявленного вызова — иначе LLM увидит
    зависший tool_call без ответа.
    """

    tool_call_id: str
    content: str


LLMFeedback: TypeAlias = LLMCritique | ToolCallRejection
