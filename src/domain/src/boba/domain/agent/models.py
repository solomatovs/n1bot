"""Модели агент-слоя.

S4a: набор вырос — появился :class:`AgentConfig` (пока только
``max_iterations``) и поле ``iteration`` в :class:`AgentContext`,
инкрементируемое :class:`~boba.domain.agent.meat.loop_control.\
IterationCounterMiddleware` на каждой итерации цикла.

Tool-специфичные поля (``workspace_id``, каталог tool'ов) появятся
при миграции соответствующих middleware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from boba.domain.llm.models import LLMRequestBuilder, RequestId

if TYPE_CHECKING:
    from boba.domain.agent.next_turn import NextTurnIntent


@dataclass(frozen=True)
class AgentRequest:
    """Входные данные для одного прогона агента.

    ``model`` живёт здесь, а не в глобальном конфиге: выбирает caller
    (UI/CLI), чтобы системный дефолт не просачивался в loop незаметно
    — поведение совпадает со старой версией.
    """

    query: str
    model: str
    request_id: RequestId


@dataclass(frozen=True)
class AgentConfig:
    """Настройки одного прогона агента.

    Отделён от :class:`AgentRequest`, потому что загружается из TOML/env
    однократно и шарится между прогонами; ``AgentRequest`` — per-call.

    ``max_consecutive_tool_calls`` — лимит подряд идущих идентичных
    tool_call'ов (по ``(tool_name, arguments)``). Используется
    :class:`~boba.domain.agent.meat.tools.\
RepeatedToolCallGuardMiddleware` — N+1-й вызов подавляется как
    :class:`~boba.domain.agent.errors.ToolFeedbackError`.

    ``max_consecutive_format_failures`` — лимит подряд идущих
    ошибок формата content-tool-call. Используется
    :class:`~boba.domain.agent.meat.tools.\
RepeatedFormatFailureGuardMiddleware` — после N+1 эмитится
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

    Middleware-стадии (prompt, tools, sampling, history) заполняют
    слоты :attr:`request`; терминальная стадия
    (:class:`~boba.domain.agent.meat.llm_invoke.LLMInvokeMiddleware`)
    на границе с LLM-слоем валидирует запрос и передаёт его в LLM.

    :attr:`request` **локален для одной итерации** цикла агента: на
    каждой итерации :class:`~boba.domain.agent.meat.loop_control.\
IterationCounterMiddleware` пересоздаёт его. Это принципиальная
    разница со старым кодом, где request жил через все итерации как
    shared mutable state.

    ``iteration`` — 1-based номер текущей итерации; ``0`` означает «до
    первой итерации» (счётчик ещё не сработал).
    """

    agent_request: AgentRequest
    config: AgentConfig = field(default_factory=AgentConfig)
    iteration: int = 0
    request: LLMRequestBuilder = field(default_factory=LLMRequestBuilder)
    _next_turn: NextTurnIntent | None = None

    def set_next_turn(self, intent: NextTurnIntent) -> None:
        """Декларирует intent на следующую итерацию.

        Политика слияния: если slot уже занят — выигрывает больший
        :meth:`NextTurnIntent.priority`. При равном приоритете —
        last-wins (последний producer перезаписывает).
        """
        current = self._next_turn
        if current is None or intent.priority() >= current.priority():
            self._next_turn = intent

    def consume_next_turn(self) -> NextTurnIntent | None:
        """Возвращает intent и освобождает slot (одноразовое чтение)."""
        intent = self._next_turn
        self._next_turn = None
        return intent
