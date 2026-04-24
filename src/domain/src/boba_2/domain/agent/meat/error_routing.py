from __future__ import annotations

from collections.abc import Iterable, Iterator

from boba.domain.core.errors import (
    LLMFeedbackError,
    RoutableError,
    UserFeedbackError,
)
from boba.domain.core.patterns import StreamSource
from boba_2.domain.agent.events import AgentEvent
from boba_2.domain.agent.messages import MessageService
from boba_2.domain.agent.models import AgentContext


class AgentErrorRouter:
    """
    Маршрутизация ошбиок в приложении
    """

    def __init__(self, message_service: MessageService) -> None:
        self._message_service = message_service

    def route(
        self, ctx: AgentContext, err: RoutableError,
    ) -> Iterator[AgentEvent]:
        """
        Разобрать одну ошибку в поток событий + побочный эффект
        """
        rid = ctx.agent_request.request_id
        if not isinstance(err, UserFeedbackError):
            raise TypeError(
                f"{type(err).__name__}: RoutableError, но не унаследован от "
                "UserFeedbackError. Добавь наследование от одного из "
                "binding-бейзов (AgentTerminalError / AgentLLMFeedbackError "
                "/ AgentUserNotice).",
            ) from err

        if isinstance(err, LLMFeedbackError):
            self._message_service.add(err.to_llm_feedback())

        yield err.to_user_event(rid)

    def run_batch(
        self,
        ctx: AgentContext,
        tasks: Iterable[Iterator[AgentEvent]],
    ) -> Iterator[AgentEvent]:
        """
        Прогнать серию подзадач с batch-семантикой для
        """
        deferred: list[LLMFeedbackError] = []
        for task in tasks:
            try:
                yield from task
            except LLMFeedbackError as e:
                deferred.append(e)
        for err in deferred:
            yield from self.route(ctx, err)


class AgentErrorRouterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Верхнеуровневый try/except над всей агентской цепочкой.
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
