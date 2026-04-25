"""Модели агент-слоя."""

from __future__ import annotations

from dataclasses import dataclass, field

from boba.domain.agent.turn.trigger import TurnTriggerQueue
from boba.domain.llm.models import RequestId


@dataclass(frozen=True)
class AgentRequest:
    """Входные данные для одного прогона агента.

    ``model`` живёт здесь, а не в глобальном конфиге: выбирает caller
    (UI/CLI), чтобы системный дефолт не просачивался в loop незаметно.
    """

    query: str
    model: str
    request_id: RequestId


@dataclass(frozen=True)
class AgentConfig:
    """Настройки одного прогона агента.

    ``max_consecutive_tool_calls`` — лимит подряд идущих идентичных
    tool_call'ов (по ``(tool_name, arguments)``). Используется
    :class:`~boba.domain.agent.meat.tools.RepeatedToolCallGuardMiddleware`:
    N+1-й вызов подавляется, declare :class:`LLMFeedbackEffect` с
    критикой в адрес модели.

    ``max_consecutive_format_failures`` — лимит подряд идущих
    ошибок формата content-tool-call. Используется
    :class:`~boba.domain.agent.meat.tools.\
RepeatedFormatFailureGuardMiddleware`: после N+1 эмитится
    :class:`~boba.domain.agent.events.RepeatedFormatFailure` и цикл
    останавливается.
    """

    max_iterations: int = 20
    max_consecutive_tool_calls: int = 3
    max_consecutive_format_failures: int = 3


@dataclass
class AgentContext:
    """Mutable контекст одного прогона, передаваемый через цепочку
    middleware.

    - :attr:`iteration` — 1-based номер текущей итерации; ``0`` означает
      «до первой итерации» (счётчик ещё не сработал).

    - :attr:`triggers` — очередь эффектов следующего turn'а,
      :class:`TurnTriggerQueue`. Producer'ы внутри итерации
      дописывают через :meth:`TurnTriggerQueue.declare`;
      :class:`LLMInvokeMiddleware` один раз за итерацию вычитывает
      через :meth:`TurnTriggerQueue.consume` и прогоняет через
      :class:`TurnSpec`.
    """

    agent_request: AgentRequest
    config: AgentConfig = field(default_factory=AgentConfig)
    iteration: int = 0
    triggers: TurnTriggerQueue = field(default_factory=TurnTriggerQueue)
