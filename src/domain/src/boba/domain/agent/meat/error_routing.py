"""Полиморфная маршрутизация :class:`RoutableError` по маркерам.

Роутер не знает конкретных подклассов — он читает маркеры
(:class:`UserFeedbackError`, :class:`LLMFeedbackError`,
:class:`TerminalError`) независимо и суммирует
эффекты. Добавление новой ошибки сводится к миксу нужных маркеров и
реализации их abstract-методов.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from boba.domain.agent.events import AgentEvent, GenericTerminalFailure
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext
from boba.domain.core.errors import (
    LLMFeedbackError,
    RoutableError,
    TerminalError,
    UserFeedbackError,
)
from boba.domain.core.patterns import StreamSource


class AgentErrorRouter:
    """Маршрутизирует :class:`RoutableError` по маркерам.

    Эффекты собираются независимо в таком порядке:

    1. :class:`LLMFeedbackError` — ``to_llm_feedback()`` пишется в
       :class:`MessageService`. LLM увидит фидбек на следующей итерации.
    2. :class:`UserFeedbackError` — ``to_user_feedback(request_id)`` yield-ится
       в stream. Sink получает событие.
    3. :class:`TerminalError` — если шаг 2 не дал события, роутер
       эмитит generic :class:`GenericTerminalFailure`, чтобы
       :class:`StopOnAnyFailure` остановил цикл. Если ``to_user_feedback``
       уже вернул подкласс ``TerminalFailure`` — повторного события
       не будет.

    Две API-точки:

    - :meth:`route` — разобрать одну ошибку.
    - :meth:`run_batch` — прогнать серию подзадач с batch-семантикой:
      не-терминальные ошибки копятся и маршрутизируются в конце,
      :class:`TerminalError` пропускается наверх немедленно.
    """

    def __init__(self, message_service: MessageService) -> None:
        self._message_service = message_service

    def route(
        self,
        ctx: AgentContext,
        err: RoutableError,
    ) -> Iterator[AgentEvent]:
        rid = ctx.agent_request.request_id

        if isinstance(err, LLMFeedbackError):
            self._message_service.add(err.to_llm_feedback())

        has_event = False
        if isinstance(err, UserFeedbackError):
            yield err.to_user_feedback(rid)
            has_event = True

        if isinstance(err, TerminalError) and not has_event:
            yield GenericTerminalFailure(
                request_id=rid,
                error_kind=type(err).__name__,
                message=str(err),
            )

    def run_batch(
        self,
        ctx: AgentContext,
        tasks: Iterable[Iterator[AgentEvent]],
    ) -> Iterator[AgentEvent]:
        """Серия подзадач с batch-семантикой для не-терминальных ошибок.

        События успешных подзадач стримятся сразу. Не-терминальные
        :class:`RoutableError` из подзадач копятся и маршрутизируются
        после завершения всей серии (в порядке возникновения).
        :class:`TerminalError` пропускается наверх немедленно — batch
        прерывается.
        """
        deferred: list[RoutableError] = []
        for task in tasks:
            try:
                yield from task
            except TerminalError:
                raise
            except RoutableError as e:
                deferred.append(e)
        for err in deferred:
            yield from self.route(ctx, err)


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
