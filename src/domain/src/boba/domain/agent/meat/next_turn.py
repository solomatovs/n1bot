"""NextTurnApplyMiddleware — трансляция NextTurnIntent → MessageService + builder.

НАБРОСОК. Не подключен в композицию. Демонстрирует форму того, как
единая декларация :class:`~boba.domain.agent.next_turn.NextTurnIntent`
превращается в конкретные эффекты на границе с
:class:`MessageService` и ``ctx.request``.

Ставится в цепочке ПОСЛЕ :class:`IterationCounterMiddleware` и
:class:`NewLLMRequestMiddleware` (новый builder уже создан), ПЕРЕД
:class:`SystemPromptMiddleware` / ``UserPromptMiddleware`` — чтобы
intent мог заполнить ``user_message`` до того, как провайдеры
попытаются это сделать (в текущей схеме ``UserPromptMiddleware``
должен будет узнавать "intent уже заполнил — я не перезаписываю"
либо вообще уйти).

Жизненный цикл:

1. ``Agent.run`` создаёт ctx и ставит
   ``ctx.set_next_turn(UserQueryIntent(request.query))``.
2. На каждой итерации apply-middleware консьюмит intent (slot
   обнуляется после чтения).
3. Producer'ы в конце итерации декларируют новый intent через
   ``ctx.set_next_turn(...)``:

   - ``ToolExecutionMiddleware`` после успешного batch'а →
     ``ToolResultsIntent(tool_call_count=len(pending))``;
   - ``AgentErrorRouter`` при :class:`LLMFeedbackError` уровня
     LLM/prompt/iteration → ``LLMFeedbackIntent(message=...)``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import assert_never

from boba.domain.agent.events import AgentEvent, UserQueryReceived
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext
from boba.domain.agent.next_turn import (
    LLMFeedbackIntent,
    NextTurnIntent,
    ToolResultsIntent,
    UserQueryIntent,
)
from boba.domain.core.patterns import StreamSource
from boba.domain.llm.models import LLMMessage


class NextTurnApplyMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Консьюмит ``ctx.next_turn`` и транслирует в MessageService + builder."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._message_service = message_service
        self._sticky_user_query: str | None = None

    def name(self) -> str:
        return "NextTurnApply"

    def reset(self) -> None:
        self._sticky_user_query = None
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        intent = ctx.consume_next_turn()
        if intent is None:
            raise RuntimeError(
                f"NextTurnIntent не выставлен на итерации {ctx.iteration}. "
                "Agent.run обязан инициализировать UserQueryIntent; "
                "последующие итерации — получать intent от tool/error "
                "producer'ов."
            )

        yield from self._apply(ctx, intent)
        yield from self._inner.stream(ctx)

    def _apply(
        self,
        ctx: AgentContext,
        intent: NextTurnIntent,
    ) -> Iterable[AgentEvent]:
        rid = ctx.agent_request.request_id

        match intent:
            case UserQueryIntent(query=q):
                self._sticky_user_query = q
                self._message_service.add(LLMMessage(role="user", content=q))
                ctx.request.set_user_message(q)
                yield UserQueryReceived(request_id=rid, query=q)

            case ToolResultsIntent():
                if self._sticky_user_query is not None:
                    ctx.request.set_user_message(self._sticky_user_query)

            case LLMFeedbackIntent(message=m):
                self._message_service.add(m)
                if self._sticky_user_query is not None:
                    ctx.request.set_user_message(self._sticky_user_query)

            case _:
                assert_never(intent)
