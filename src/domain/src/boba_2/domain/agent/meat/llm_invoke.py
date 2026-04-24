"""Граница Agent ↔ LLM — :class:`LLMInvokeMiddleware`.

**Терминальная** стадия агент-цепочки: валидирует ``ctx.request``,
создаёт :class:`LLMContext` с пробросом ``request_id`` для
трассировки и передаёт управление в LLM-слой. Перекодирует
:class:`LLMEvent` → :class:`AgentEvent` через match-case; события без
агент-аналога отбрасываются. Ловит :class:`LLMError` и превращает в
терминальный :class:`GenerationFailed`.

Не стадия middleware в смысле «оборачивает inner» — inner у неё нет.
Это осознанное решение: мост между слоями должен быть единственным и
предсказуемым, а не одним из звеньев onion-цепочки.

user-сообщение **не персистится в MessageService**: слот
``ctx.request.user_message`` — транзиентный per-iter, заполняется
каждую итерацию через :class:`UserPromptMiddleware` (дефолт) или
feedback-стадии (override). В MS живут только assistant+tool —
durable-история, которая через :class:`HistoryMiddleware` возвращается
в ``ctx.request.history_messages`` на следующих итерациях.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import ClassVar

from boba.domain.core.patterns import StreamSource
from boba_2.domain.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationFailed,
    GenerationStarted,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
)
from boba_2.domain.agent.events import LLMRequestSent as AgentLLMRequestSent
from boba_2.domain.agent.models import AgentContext
from boba_2.domain.llm.errors import LLMError, RetryableLLMError
from boba_2.domain.llm.events import (
    LLMAnswerStarted,
    LLMAnswerToken,
    LLMEvent,
    LLMGenerationDone,
    LLMGenerationStarted,
    LLMRequestSent,
    LLMThinkingStarted,
    LLMThinkingToken,
    LLMToolCallArgumentDelta,
    LLMToolCallBegin,
)
from boba_2.domain.llm.models import LLMContext


class LLMInvokeMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Терминал агент-цепочки, вызывающий LLM-слой.

    ``llm_source`` — :class:`StreamSource[LLMContext, LLMEvent]`,
    composed-цепочка LLM-middleware (retry/cache/… → terminal).
    LLMInvoke не использует :class:`LLMAgent`-оркестратор, потому что
    события должны протечь через трансляцию в :class:`AgentEvent`, а
    не осесть в его sink'е; через интерфейс ``StreamSource`` контракт
    остаётся либеральным, и в тестах можно подставить любой stub.
    """

    def __init__(
        self, llm_source: StreamSource[LLMContext, LLMEvent],
    ) -> None:
        self._llm_source = llm_source

    def name(self) -> str:
        return "LLMInvoke"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        if ctx.request.model is None:
            ctx.request.model = ctx.agent_request.model
        ctx.request.validate()

        llm_ctx = LLMContext(
            request=ctx.request,
            request_id=ctx.agent_request.request_id,
        )

        try:
            for llm_event in self._llm_source.stream(llm_ctx):
                translated = self._translate(llm_event)
                if translated is not None:
                    yield translated
        except LLMError as e:
            yield GenerationFailed(
                request_id=ctx.agent_request.request_id,
                error_kind=type(e).__name__,
                message=str(e),
                retryable=isinstance(e, RetryableLLMError),
                status_code=e.status_code,
            )

    @classmethod
    def _translate(cls, event: LLMEvent) -> AgentEvent | None:
        """LLMEvent → AgentEvent | None.

        Разбито на две группы (lifecycle + delta) для читаемости и
        соответствия лимитам сложности функции. Возвращает ``None``
        для событий без агент-аналога (``LLMRefusalToken``,
        ``LLMRetryAttempt``) — они появятся при миграции
        соответствующих стадий (refusal handling, retry UI).
        """
        translated = cls._translate_lifecycle(event)
        if translated is not None:
            return translated
        return cls._translate_delta(event)

    # LLMLifecycleMarker без дополнительных полей: 1-к-1 замена класса.
    # Callable вместо type[AgentEvent], т.к. type[Union[...]] у pyright
    # требует удовлетворения __init__ всех членов объединения.
    _LIFECYCLE_SIMPLE: ClassVar[
        dict[type[LLMEvent], Callable[..., AgentEvent]]
    ] = {
        LLMGenerationStarted: GenerationStarted,
        LLMThinkingStarted: ThinkingStarted,
        LLMAnswerStarted: AnswerStarted,
    }

    @classmethod
    def _translate_lifecycle(cls, event: LLMEvent) -> AgentEvent | None:
        simple = cls._LIFECYCLE_SIMPLE.get(type(event))
        if simple is not None:
            return simple(request_id=event.request_id)
        return cls._translate_lifecycle_with_payload(event)

    @staticmethod
    def _translate_lifecycle_with_payload(
        event: LLMEvent,
    ) -> AgentEvent | None:
        match event:
            case LLMRequestSent(
                request_id=rid,
                model=m,
                messages_count=mc,
                has_tools=ht,
            ):
                return AgentLLMRequestSent(
                    request_id=rid,
                    model=m,
                    messages_count=mc,
                    has_tools=ht,
                )
            case LLMToolCallBegin(
                request_id=rid, index=i, tool_call_id=tid, tool_name=tn,
            ):
                return ToolCallBegin(
                    request_id=rid,
                    index=i,
                    tool_call_id=tid,
                    tool_name=tn,
                )
            case LLMGenerationDone(request_id=rid, finish_reason=fr):
                return GenerationDone(request_id=rid, finish_reason=fr)
            case _:
                return None

    @staticmethod
    def _translate_delta(event: LLMEvent) -> AgentEvent | None:
        match event:
            case LLMThinkingToken(request_id=rid, token=t):
                return ThinkingToken(request_id=rid, token=t)
            case LLMAnswerToken(request_id=rid, token=t):
                return AnswerToken(request_id=rid, token=t)
            case LLMToolCallArgumentDelta(
                request_id=rid, index=i, arguments=a,
            ):
                return ToolCallArgumentDelta(
                    request_id=rid, index=i, arguments=a,
                )
            case _:
                return None
