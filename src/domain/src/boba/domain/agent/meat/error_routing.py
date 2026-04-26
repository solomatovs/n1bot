"""Полиморфная маршрутизация :class:`RoutableError` по маркерам.

Роутер не знает конкретных подклассов — он читает маркеры
(:class:`UserFeedbackError`, :class:`LLMFeedbackError`) независимо и
суммирует эффекты.

:class:`TerminalError` — специализация :class:`UserFeedbackError`,
поэтому отдельной ветки не требует: «терминальность» кодируется
типом возвращаемого события (наследник ``Terminal``), который
роутер просто yield-ит как любое user-событие.

:class:`LLMFeedbackError` пишется в :class:`MessageService` через
:class:`DialogueWriter` — на следующей итерации модель увидит
feedback в истории. Параллельно роутер эмитит
:class:`FeedbackToLLMAdded` — это снапшот-событие, фиксирующее факт
записи в диалог (инвариант «снапшот на каждую запись в MessageService»).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Literal

from boba.domain.agent.dialogue_writer import DialogueWriter
from boba.domain.agent.errors import LLMToolCallFormatError
from boba.domain.agent.events import AgentEvent, FeedbackToLLMAdded
from boba.domain.agent.models import AgentContext
from boba.domain.core.errors import (
    LLMFeedbackError,
    RoutableError,
    UserFeedbackError,
)
from boba.domain.core.patterns import StreamSource
from boba.domain.llm.models import LLMMessage, RequestId


def _cause_for_feedback_error(
    err: LLMFeedbackError[LLMMessage],
) -> Literal["tool_format", "other"]:
    """Маппинг concrete-ошибки в ``cause`` для :class:`FeedbackToLLMAdded`.

    Жестко заданный диспетчер: error_routing — agent-domain модуль,
    ему позволено знать concrete agent-errors. Если появятся новые
    feedback-ошибки — добавляются ветки.
    """
    if isinstance(err, LLMToolCallFormatError):
        return "tool_format"
    return "other"


class AgentErrorRouter:
    """Маршрутизирует :class:`RoutableError` по маркерам.

    Эффекты собираются независимо:

    1. :class:`LLMFeedbackError` — пишется в :class:`MessageService`
       через :class:`DialogueWriter`; параллельно эмитится
       :class:`FeedbackToLLMAdded` (снапшот для sink'а / history-
       реконструкции). LLM увидит feedback на следующей итерации.
    2. :class:`UserFeedbackError` — ``to_user_feedback(request_id)``
       yield-ится в stream. Sink получает событие; если событие —
       наследник ``Terminal`` (как у :class:`TerminalError`-ошибок),
       :class:`StopOnAnyFailure` остановит цикл.
    """

    def __init__(self, writer: DialogueWriter) -> None:
        self._writer = writer

    def route(
        self,
        ctx: AgentContext,
        err: RoutableError,
    ) -> Iterator[AgentEvent]:
        rid: RequestId = ctx.agent_request.request_id

        if isinstance(err, LLMFeedbackError):
            feedback = err.to_llm_feedback()
            self._writer.append_llm_feedback(feedback)
            yield FeedbackToLLMAdded(
                request_id=rid,
                content=feedback.content,
                cause=_cause_for_feedback_error(err),
            )

        if isinstance(err, UserFeedbackError):
            yield err.to_user_feedback(rid)


class AgentErrorRouterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Top-level try/except над всей агентской цепочкой.

    Ставится самым внешним слоем. Любой middleware глубже может
    ``raise`` подкласс :class:`RoutableError` — этот middleware
    ловит и делегирует :class:`AgentErrorRouter`.

    Всё, что не наследует :class:`RoutableError` (``KeyError``,
    ``TypeError`` и т.п.), проходит насквозь и крашит процесс — баги
    не маскируем.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        router: AgentErrorRouter,
    ) -> None:
        self._inner = inner
        self._router = router

    def name(self) -> str:
        return "AgentErrorRouter"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        try:
            yield from self._inner.stream(ctx)
        except RoutableError as e:
            yield from self._router.route(ctx, e)
