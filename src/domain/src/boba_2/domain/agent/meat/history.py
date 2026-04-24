"""HistoryMiddleware — перенос диалога из :class:`MessageService` в
:class:`~boba_2.domain.llm.models.LLMRequest` на каждой итерации.

Ставится **внутри** цикла (после :class:`IterationCounterMiddleware`,
который пересоздаёт ``ctx.request``), перед терминалом. Каждая
итерация начинается с пустого request'а, и эта стадия наполняет
``history_messages`` срезом всей истории диалога из
:class:`MessageService` — в порядке её записи.

Отсюда вытекает семантика обновления истории: persistence-стадии
(:class:`~boba_2.domain.agent.meat.llm_invoke.\
UserMessagePersistenceMiddleware` перед отправкой,
:class:`~boba_2.domain.agent.meat.dialogue.\
AssistantMessagePersistenceMiddleware` после получения ответа,
:class:`~boba_2.domain.agent.meat.tools.ToolExecutionMiddleware`
после исполнения tool) **кладут сообщение в MessageService один раз
и больше не трогают request**. В каждой следующей итерации
``HistoryMiddleware`` сам «раскатывает» накопленное в текущий
снапшот запроса.

Собственных событий не эмитит.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.domain.core.patterns import StreamSource
from boba_2.domain.agent.events import AgentEvent
from boba_2.domain.agent.messages import MessageService
from boba_2.domain.agent.models import AgentContext


class HistoryMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Заполняет ``ctx.request.history_messages`` из :class:`MessageService`."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._message_service = message_service

    def name(self) -> str:
        return "History"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        ctx.request.set_history_messages(self._message_service.message_iter())
        yield from self._inner.stream(ctx)
