"""AgentLoop — оркестратор, собранный из stream-примитивов."""

from __future__ import annotations

from typing import Iterator

from boba.domain.agent.events import AgentEvent, GenerationDone
from boba.domain.agent.llm import LLMMiddleware
from boba.domain.agent.models import AgentConfig, AgentContext, AgentRequest
from boba.domain.core.stream import Loop


class AgentLoop(Loop[AgentContext, AgentEvent]):
    """
    Оркестратор агента.
    Middleware-цепочка передаётся снаружи — состав и порядок слоёв определяется в DI.
    """

    def __init__(
        self,
        config: AgentConfig,
        chain: LLMMiddleware,
    ) -> None:
        self._config = config
        super().__init__(source=chain)

    def name(self) -> str:
        return "AgentLoop"

    def run(self, request: AgentRequest) -> Iterator[AgentEvent]:
        """Удобный метод: создаёт контекст и запускает цикл."""
        yield from self.produce(
            AgentContext(
                request=request,
                config=self._config,
            )
        )

    def should_stop(self, ctx: AgentContext, event: AgentEvent) -> bool:
        if not isinstance(event, GenerationDone):
            return False

        if ctx.iteration >= ctx.config.max_iterations:
            return True

        return event.finish_reason != "tool_calls"
