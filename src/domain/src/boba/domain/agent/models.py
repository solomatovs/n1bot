"""Модели агент-слоя."""

from __future__ import annotations

from dataclasses import dataclass, field

from boba.domain.llm.models import RequestId


@dataclass(frozen=True)
class AgentRequest:
    """Параметры одного прогона агента.

    Связка per-call настроек обработки: ``model``, ``request_id``
    (и в будущем — ``temperature``, ``response_format`` и пр.).
    Само сообщение пользователя сюда **не входит** — оно передаётся
    в :meth:`Agent.run` отдельным параметром ``query`` и сразу
    кладётся в :class:`MessageService` через :class:`DialogueWriter`,
    после чего нигде в runtime-контексте не дублируется.

    ``model`` живёт здесь, а не в глобальном конфиге: выбирает caller
    (UI/CLI), чтобы системный дефолт не просачивался в loop незаметно.
    """

    model: str
    request_id: RequestId


@dataclass(frozen=True)
class AgentConfig:
    """Настройки одного прогона агента.

    ``max_consecutive_tool_calls`` — лимит подряд идущих идентичных
    tool_call'ов (по ``(tool_name, arguments)``). Используется
    :class:`~boba.domain.agent.meat.tools.RepeatedToolCallGuardMiddleware`:
    N+1-й вызов подавляется, в историю пишется feedback с
    ``role="tool"`` через :class:`DialogueWriter`.

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
    """

    agent_request: AgentRequest
    config: AgentConfig = field(default_factory=AgentConfig)
    iteration: int = 0
