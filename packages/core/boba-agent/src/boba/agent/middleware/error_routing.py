"""Полиморфная маршрутизация RoutableError по маркерам."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from boba.agent.agent import AgentContext
from boba.agent.errors import (
    AgentLLMFeedbackError,
    RoutableError,
    UserFeedbackError,
    extract_error_context,
)
from boba.agent.events import (
    AdvisoryEvent,
    AgentEvent,
    FeedbackToLLMAdded,
    TerminalEvent,
)
from boba.llm.models import RequestId
from boba.patterns import StreamSource


class AgentErrorRouter:
    """Маршрутизирует RoutableError по маркерам.

    Любое выпущенное UserFeedbackError авто-обогащается контекстом из
    самого исключения: HTTP-статус и цепочка __cause__/__context__
    подкладываются в событие, чтобы автору ошибки не нужно было
    плумбить эти подробности вручную через свой to_user_feedback.
    """

    def route(
        self,
        request_id: RequestId,
        err: RoutableError,
    ) -> Iterator[AgentEvent]:
        if isinstance(err, AgentLLMFeedbackError):
            feedback = err.to_llm_feedback()
            yield FeedbackToLLMAdded(
                request_id=request_id,
                content=feedback.content,
            )

        if isinstance(err, UserFeedbackError):
            yield _enrich_with_error_context(err.to_user_feedback(request_id), err)


def _enrich_with_error_context(
    event: AgentEvent,
    err: BaseException,
) -> AgentEvent:
    """Прокинуть status_code + cause_chain из exc в event (если событие — ошибка).

    Перегоняем через model_validate, чтобы переиграть _derive подкласса
    и пересобрать body через compose_error_body с подмешанными
    деталями. На не-error событиях — no-op.
    """
    if not isinstance(event, (TerminalEvent, AdvisoryEvent)):
        return event
    status_code, cause_chain = extract_error_context(err)
    if status_code is None and not cause_chain:
        return event
    return type(event).model_validate(
        {
            **event.model_dump(),
            "status_code": status_code,
            "cause_chain": list(cause_chain),
        },
    )


class AgentErrorRouterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Top-level try/except над агентской цепочкой; делегирует router."""

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
            yield from self._router.route(ctx.request_id, e)
