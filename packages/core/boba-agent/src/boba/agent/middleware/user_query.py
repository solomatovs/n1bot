"""UserQueryRecorderMiddleware: эмитит UserQueryReceived один раз на request.

Журнал HistoryService должен содержать запрос пользователя как первое
доменное событие, чтобы HistoryDialogView мог восстановить UserMessage
для прошлых turn'ов. Middleware ставится самым внутренним wrapper'ом —
сразу над LLMPort — и эмитит UserQueryReceived до любых LLM-событий
текущего request_id; дедуп по request_id гарантирует ровно один эмит,
даже если StreamSourceLoop зайдёт в цикл повторно.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent.agent import AgentContext
from boba.agent.events import AgentEvent, UserQueryReceived
from boba.llm.models import RequestId
from boba.patterns import StreamSource


class UserQueryRecorderMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Эмитит UserQueryReceived один раз на request_id перед inner-стримом."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
    ) -> None:
        self._inner = inner
        self._seen: set[RequestId] = set()

    def name(self) -> str:
        return "UserQueryRecorder"

    def reset(self) -> None:
        self._seen.clear()
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        if ctx.request_id not in self._seen:
            self._seen.add(ctx.request_id)
            yield UserQueryReceived(
                request_id=ctx.request_id,
                query=ctx.query,
            )
        yield from self._inner.stream(ctx)
