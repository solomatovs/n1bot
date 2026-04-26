"""Модели агент-слоя."""

from __future__ import annotations

from dataclasses import dataclass, field

from boba.domain.llm.models import RequestId, SamplingParams


@dataclass(frozen=True)
class AgentRequest:
    """Параметры одного прогона агента.

    Связка per-call настроек обработки: ``model``, ``request_id``,
    опциональный ``sampling`` (per-call override параметров генерации).
    Само сообщение пользователя сюда **не входит** — оно передаётся
    в :meth:`Agent.run` отдельным параметром ``query`` и сразу
    кладётся в :class:`MessageService` через :class:`DialogueWriter`,
    после чего нигде в runtime-контексте не дублируется.

    ``model`` живёт здесь, а не в глобальном конфиге: выбирает caller
    (UI/CLI), чтобы системный дефолт не просачивался в loop незаметно.

    ``sampling`` — единственный источник параметров sampling: глобального
    конфига нет. ``None`` означает «провайдеру не передавать ничего» —
    каждое поле :class:`SamplingParams` обрабатывается независимо в
    :class:`~boba.domain.agent.turn.reducers.AgentRequestSamplingReducer`.
    """

    model: str
    request_id: RequestId
    sampling: SamplingParams | None = None


@dataclass(frozen=True)
class AgentConfig:
    """Настройки одного прогона агента.

    ``max_consecutive_tool_calls`` — лимит подряд идущих идентичных
    tool_call'ов (по ``(tool_name, arguments)``). Используется
    :class:`~boba.domain.agent.middleware.tools.RepeatedToolCallGuardMiddleware`:
    N+1-й вызов подавляется, в историю пишется feedback с
    ``role="tool"`` через :class:`DialogueWriter`.
    """

    max_iterations: int = 20
    max_consecutive_tool_calls: int = 3


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
