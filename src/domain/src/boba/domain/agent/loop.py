"""AgentLoop — оркестратор, собранный из stream-примитивов."""

from __future__ import annotations

from typing import Iterator

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.llm import LLMMiddleware
from boba.domain.agent.models import AgentConfig, AgentContext, AgentRequest
from boba.domain.agent.stop_conditions import AgentStopCondition
from boba.domain.core.patterns import Loop


class AgentLoop(Loop[AgentContext, AgentEvent]):
    """
    Оркестратор агента.
    Middleware-цепочка и условия остановки передаются снаружи через DI.
    """

    def __init__(
        self,
        config: AgentConfig,
        chain: LLMMiddleware,
        stop: AgentStopCondition,
    ) -> None:
        self._config = config
        super().__init__(source=chain, stop=stop)

    def name(self) -> str:
        return "AgentLoop"

    def run(self, request: AgentRequest) -> Iterator[AgentEvent]:
        """создаёт контекст и запускает цикл."""
        yield from self.produce(
            AgentContext(
                request=request,
                config=self._config,
            )
        )
