"""Полиморфная маршрутизация RoutableError по маркерам.

Роутер не знает конкретных подклассов — читает маркеры независимо и
суммирует эффекты.

TerminalError — специализация UserFeedbackError; «терминальность»
кодируется типом возвращаемого события (Terminal), роутер yield-ит его
как обычное user-событие.

AgentLLMFeedbackError — agent-уровневая специализация LLMFeedbackError
с зафиксированным TFeedback=LLMMessage. Роутер пишет в MessageService
через DialogueWriter и параллельно эмитит FeedbackToLLMAdded.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import assert_never

from boba.domain.agent.dialogue_writer import DialogueWriter
from boba.domain.agent.errors import AgentLLMFeedbackError
from boba.domain.agent.events import AgentEvent, FeedbackToLLMAdded
from boba.domain.agent.models import AgentContext
from boba.domain.agent.payloads import LLMCritique, LLMFeedback, ToolCallRejection
from boba.domain.core.errors import RoutableError, UserFeedbackError
from boba.domain.core.patterns import StreamSource
from boba.domain.llm.models import RequestId


class AgentErrorRouter:
    """Маршрутизирует RoutableError по маркерам.

    Эффекты собираются независимо:

    1. AgentLLMFeedbackError — `to_llm_feedback()` отдаёт
       LLMFeedback-union, который match-диспетчер раскрывает
       в узкие методы DialogueWriter. Параллельно эмитится
       FeedbackToLLMAdded (снапшот для sink'а / history-
       реконструкции). LLM увидит feedback на следующей итерации.
    2. UserFeedbackError — to_user_feedback(request_id)
       yield-ится в stream. Sink получает событие; если событие —
       наследник Terminal (как у TerminalError-ошибок),
       StopOnAnyFailure остановит цикл.
    """

    def __init__(self, writer: DialogueWriter) -> None:
        self._writer = writer

    def route(
        self,
        ctx: AgentContext,
        err: RoutableError,
    ) -> Iterator[AgentEvent]:
        rid: RequestId = ctx.agent_request.request_id

        if isinstance(err, AgentLLMFeedbackError):
            feedback = err.to_llm_feedback()
            self._dispatch_feedback(feedback)
            yield FeedbackToLLMAdded(
                request_id=rid,
                content=feedback.content,
            )

        if isinstance(err, UserFeedbackError):
            yield err.to_user_feedback(rid)

    def _dispatch_feedback(self, feedback: LLMFeedback) -> None:
        """Раскрывает LLMFeedback-union в узкие writer-методы.

        assert_never в default-ветке гарантирует exhaustiveness:
        при добавлении нового варианта в LLMFeedback pyright
        потребует обработать его здесь.
        """
        match feedback:
            case LLMCritique(content=c):
                self._writer.append_llm_critique(c)
            case ToolCallRejection(tool_call_id=tid, content=c):
                self._writer.append_tool_call_rejection(
                    tool_call_id=tid, content=c,
                )
            case _:
                assert_never(feedback)


class AgentErrorRouterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Top-level try/except над всей агентской цепочкой.

    Ставится самым внешним слоем. Любой middleware глубже может
    raise подкласс RoutableError — этот middleware
    ловит и делегирует AgentErrorRouter.

    Всё, что не наследует RoutableError (KeyError,
    TypeError и т.п.), проходит насквозь и крашит процесс — баги
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
