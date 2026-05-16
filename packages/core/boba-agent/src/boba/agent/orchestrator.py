"""Оркестратор агента: stream — поток событий, invoke — прогон до итога."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from boba.agent.events import AgentEvent, GenerationDone, IterationStarted
from boba.agent.messages import MessageReader, MessageWriter
from boba.llm.events import FinishReason
from boba.llm.models import Message, RequestId, SamplingParams, UserMessage
from boba.patterns import StreamSource


@dataclass(frozen=True)
class AgentRequest:
    """Параметры одного прогона агента (model, request_id, sampling)."""

    request_id: RequestId
    model: str
    query: str
    sampling: SamplingParams | None = None


class AgentConfig(BaseModel):
    """Конфиг агентского цикла.

    Сейчас пустой — лимиты (max_iterations, max_consecutive_tool_calls)
    переехали в bootstrap конкретных middleware (`IterationCounterMiddleware`,
    `RepeatedToolCallGuardMiddleware`). Класс остаётся как заглушка для
    `AgentInput.config` / `AgentContext.config` на случай будущих сквозных
    параметров run-а.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True)
class AgentContext:
    """Контекст одного прогона: input-данные, неизменяемые в течение run."""

    request: AgentRequest
    config: AgentConfig = field(default_factory=AgentConfig)


@dataclass(frozen=True)
class AgentInput:
    """Вход одного прогона агента: query + параметры запроса."""

    request: AgentRequest
    config: AgentConfig = field(default_factory=AgentConfig)


@dataclass(frozen=True)
class AgentRunResult:
    """Итог одного прогона агента (для invoke)."""

    final_message: Message | None
    iterations: int
    finish_reason: FinishReason | None


class Agent:
    """Тонкий оркестратор: пишет user-query, прогоняет source, отдаёт события."""

    def __init__(
        self,
        source: StreamSource[AgentContext, AgentEvent],
        writer: MessageWriter,
        reader: MessageReader,
    ) -> None:
        self._source = source
        self._writer = writer
        self._reader = reader

    def name(self) -> str:
        return "Agent"

    def stream(self, agent_input: AgentInput) -> Iterator[AgentEvent]:
        """Прогнать агента; ленивый итератор AgentEvent. Не raise — события."""
        self._source.reset()
        self._writer.add(UserMessage(content=agent_input.request.query))
        ctx = AgentContext(
            request=agent_input.request,
            config=agent_input.config,
        )
        yield from self._source.stream(ctx)

    def invoke(self, agent_input: AgentInput) -> AgentRunResult:
        """Прогнать агента до конца; вернуть AgentRunResult."""
        iterations = 0
        finish_reason: FinishReason | None = None
        for event in self.stream(agent_input):
            if isinstance(event, IterationStarted):
                iterations = event.iteration_count
            elif isinstance(event, GenerationDone):
                finish_reason = event.finish_reason
        return AgentRunResult(
            final_message=self._reader.last(),
            iterations=iterations,
            finish_reason=finish_reason,
        )
