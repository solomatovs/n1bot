"""DialogueWriter — единственный писатель в :class:`MessageService`.

Все фиксации в историю диалога внутри агентского цикла идут через
этот фасад. Это даёт инвариант «один писатель», который снимает
размазанную ответственность между middleware и упрощает рассуждения
о порядке записей.

Семантика методов привязана к доменным событиям, а не к
:class:`LLMMessage` напрямую: caller думает в терминах «что
произошло» (пользователь прислал query, модель ответила, тул вернул
результат, ошибка превратилась в feedback), а writer кодирует это в
правильный ``role``/payload.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.domain.agent.messages import MessageService
from boba.domain.llm.models import LLMMessage, LLMToolCall


class DialogueWriter:
    """Тонкий фасад поверх :class:`MessageService`.

    Внутри агентского цикла никто не должен звать ``message_service.add()``
    напрямую — все записи идут через этот объект.
    """

    def __init__(self, service: MessageService) -> None:
        self._service = service

    def append_user_query(self, content: str) -> None:
        """Первое сообщение пользователя.

        Вызывается :class:`~boba.domain.agent.meat.agent.Agent` ровно
        один раз на старте прогона, до запуска stream'а. ``content``
        — уже отформатированный текст: обогащение (IDE selection,
        шаблоны, контекст вызова) — ответственность caller'а
        (frontend/CLI), не агента.
        """
        self._service.add(LLMMessage(role="user", content=content))

    def append_assistant(
        self,
        content: str,
        tool_calls: Iterable[LLMToolCall],
    ) -> None:
        """Ответ модели после :class:`GenerationDone`."""
        self._service.add(
            LLMMessage(
                role="assistant",
                content=content,
                tool_calls=tuple(tool_calls),
            ),
        )

    def append_tool_result(self, tool_call_id: str, content: str) -> None:
        """Результат выполнения tool_call.

        И успех, и ошибка идут одним путём ``role="tool"`` — это всё
        ещё «результат вызова инструмента», просто с негативным
        payload'ом в случае ошибки.
        """
        self._service.add(
            LLMMessage(
                role="tool",
                content=content,
                tool_call_id=tool_call_id,
            ),
        )

    def append_llm_feedback(self, message: LLMMessage) -> None:
        """Готовый feedback-message от роутера ошибок / guard'а лупов.

        Роль и формат сообщения определяет источник через
        ``err.to_llm_feedback()`` (обычно ``role="user"`` — критика
        парсера, или ``role="tool"`` — подавление дублирующего
        вызова). Writer не интерпретирует payload — пишет как есть.
        """
        self._service.add(message)
