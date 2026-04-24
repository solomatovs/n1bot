"""TurnSpec — fold-фабрика, собирающая LLMRequest из reducer'ов.

Собирается один раз в контейнере: подключаются нужные
reducer'ы, спек готов к многократному build'у.

Per-call:

1. Caller формирует :class:`TurnResolveContext` из
   ``AgentContext`` + consumed :class:`TurnTrigger` +
   :class:`MessageService`.
2. ``spec.build(resolve_ctx)`` → :class:`LLMRequest`.

Что происходит внутри :meth:`build`:

- :meth:`initial` применяет все effects trigger'а к
  :class:`MessageService` (единственная точка записи в диалог);
  возвращает пустой :class:`TurnState`.
- Reducer'ы по возрастанию ``priority()`` заполняют slots state'а
  (model, system, history, tools, sampling).
- :meth:`finalize` валидирует заполненность обязательных slot'ов
  и возвращает immutable :class:`LLMRequest`.

LEGACY-переходник: в текущей версии :class:`LLMRequest` требует
отдельное поле ``user_message``. Пока оно не удалено, :meth:`finalize`
берёт последнее сообщение истории как ``user_message``, остальное
отправляет в ``history_messages``. В коммите, который вырежет
``user_message`` из :class:`LLMRequest`, этот хак уйдёт.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext
from boba.domain.agent.turn.trigger import TurnTrigger
from boba.domain.core.patterns import ContextFoldFactory, StrId
from boba.domain.llm.errors import (
    LLMRequestModelNoneError,
    LLMRequestSystemMessageNoneError,
    LLMRequestUserMessageNoneError,
)
from boba.domain.llm.models import (
    LLMMessage,
    LLMRequest,
    LLMToolRequest,
    SamplingParams,
)


@dataclass
class TurnState:
    """Частично собранный :class:`LLMRequest`, накапливается reducer'ами."""

    model: str | None = None
    system_message: LLMMessage | None = None
    history_messages: tuple[LLMMessage, ...] = ()
    tools: LLMToolRequest = field(default_factory=LLMToolRequest)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class TurnResolveContext:
    """Контекст одного build'а TurnSpec.

    ``agent`` — mutable :class:`AgentContext` одной итерации
    (request_id, iteration, config).
    ``trigger`` — уже consumed из :attr:`AgentContext._pending`
    (resolver получает его явно, чтобы фабрика не мутировала
    AgentContext).
    ``messages`` — сервис истории; :meth:`TurnSpec.initial` пишет в
    него effects.
    """

    agent: AgentContext
    trigger: TurnTrigger
    messages: MessageService


class TurnSpec(
    ContextFoldFactory[TurnResolveContext, StrId, TurnState, LLMRequest],
):
    def initial(self, ctx: TurnResolveContext) -> TurnState:
        for effect in ctx.trigger.effects:
            effect.apply(ctx.messages)
        return TurnState()

    def finalize(self, ctx: TurnResolveContext, state: TurnState) -> LLMRequest:
        if state.model is None:
            raise LLMRequestModelNoneError()
        if state.system_message is None:
            raise LLMRequestSystemMessageNoneError()
        if not state.history_messages:
            raise LLMRequestUserMessageNoneError()

        *head, tail = state.history_messages
        return LLMRequest(
            model=state.model,
            system_message=state.system_message,
            user_message=tail,
            history_messages=tuple(head),
            tools=state.tools,
            sampling=state.sampling,
            response_format=state.response_format,
        )
