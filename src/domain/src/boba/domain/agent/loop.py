"""AgentLoop — оркестратор, собранный из stream-примитивов."""

from __future__ import annotations

from typing import Iterator

from boba.domain.agent.events import AgentEvent, GenerationDone
from boba.domain.agent.models import AgentConfig, AgentContext, AgentRequest
from boba.domain.agent.stages import GenerateStage
from boba.domain.llm.llm import LLMCompletionService
from boba.domain.core.messages import MessageService
from boba.domain.core.stream import Loop, Pipeline


class AgentLoop(Loop[AgentContext, AgentEvent]):
    """
    Оркестратор агента.
    """

    def __init__(
        self,
        config: AgentConfig,
        message_service: MessageService,
        llm: LLMCompletionService,
    ) -> None:
        self._config = config
        self._message_service = message_service
        
        super().__init__(
            source=Pipeline([
                GenerateStage(llm, message_service),
            ]),
        )

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
            """Останавливаемся только по итогам генерации"""
            return False

        if ctx.iteration >= ctx.config.max_iterations:
            """Остановиться, если достигнут лимит итераций."""
            return True

        return False
