"""AgentLoop — оркестратор, собранный из stream-примитивов."""

from __future__ import annotations

from typing import Iterator

from boba.domain.agent.events import AgentEvent, GenerationDone
from boba.domain.agent.models import AgentConfig, AgentContext, AgentRequest
from boba.domain.core.stream import Loop, Pipeline


class AgentLoop(Loop[AgentContext, AgentEvent]):
    """
    Оркестратор агента.
    Pipeline передаётся снаружи — состав и порядок стадий определяется в DI.
    """

    def __init__(
        self,
        config: AgentConfig,
        pipeline: Pipeline[AgentContext, AgentEvent],
    ) -> None:
        self._config = config
        super().__init__(source=pipeline)

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

        # tool_calls → продолжаем (ToolExecutionStage выполнит и цикл повторится)
        # stop, length → останавливаемся
        return event.finish_reason != "tool_calls"
