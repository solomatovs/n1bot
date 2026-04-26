"""DialogueWriter — единственный писатель в историю сообщений.

Семантика методов привязана к доменным событиям, а не к
LLMMessage напрямую: caller думает в терминах «что
произошло» (пользователь прислал query, модель ответила, тул вернул
результат, ошибка превратилась в feedback), а writer кодирует это в
правильный role/payload и пишет через MessageWriter-порт.

Инвариант «один писатель» гарантируется типами: единственный, кому
DI отдаёт MessageWriter — это сам DialogueWriter. Все
остальные consumer'ы получают только MessageReader, который
не умеет add.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.domain.agent.messages import MessageWriter
from boba.domain.llm.models import LLMMessage, LLMToolCall


class DialogueWriter:
    """Доменный фасад поверх MessageWriter.

    Принимает MessageWriter (write-side только). Маппит
    доменные операции в LLMMessage-структуры и пишет.
    """

    def __init__(self, writer: MessageWriter) -> None:
        self._writer = writer

    def append_user_query(self, content: str) -> None:
        """Первое сообщение пользователя.

        Вызывается Agent ровно
        один раз на старте прогона, до запуска stream'а. content
        — уже отформатированный текст: обогащение (IDE selection,
        шаблоны, контекст вызова) — ответственность caller'а
        (frontend/CLI), не агента.
        """
        self._writer.add(LLMMessage(role="user", content=content))

    def append_assistant(
        self,
        content: str,
        tool_calls: Iterable[LLMToolCall],
    ) -> None:
        """Ответ модели после GenerationDone."""
        self._writer.add(
            LLMMessage(
                role="assistant",
                content=content,
                tool_calls=tuple(tool_calls),
            ),
        )

    def append_tool_result(self, tool_call_id: str, content: str) -> None:
        """Результат выполнения tool_call.

        И успех, и ошибка идут одним путём role="tool" — это всё
        ещё «результат вызова инструмента», просто с негативным
        payload'ом в случае ошибки.
        """
        self._writer.add(
            LLMMessage(
                role="tool",
                content=content,
                tool_call_id=tool_call_id,
            ),
        )

    def append_llm_critique(self, content: str) -> None:
        """Общая критика к LLM (role="user").

        Не привязана к конкретному tool_call'у. Используется для
        feedback'а, относящегося к нарушению протокола / формата
        ответа в целом (например, плохо построенный JSON).

        Контракт writer'а: только примитивы на входе — LLMMessage
        собирается здесь. Внешним вызывающим код-путям нельзя
        прокинуть готовый LLMMessage.
        """
        self._writer.add(LLMMessage(role="user", content=content))

    def append_tool_call_rejection(
        self,
        *,
        tool_call_id: str,
        content: str,
    ) -> None:
        """Подавление tool_call'а: синтетический ответ в его слот
        (role="tool" + tool_call_id).

        Используется guard'ами, которые **не запускали** tool, но
        обязаны заполнить слот объявленного вызова — иначе LLM увидит
        зависший tool_call без ответа. Содержание — критика причины
        подавления.

        Семантически отличается от append_tool_result: тот
        фиксирует «мы запустили tool и получили X», этот — «мы НЕ
        запускали; вот критика».
        """
        self._writer.add(
            LLMMessage(role="tool", content=content, tool_call_id=tool_call_id),
        )
