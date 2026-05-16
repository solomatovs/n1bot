"""Оркестратор агента: stream — поток событий, invoke — текст итогового ответа."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from boba.agent.events import AgentEvent, AnswerComplete
from boba.llm.models import RequestId, new_request_id
from boba.patterns import StreamSource


@dataclass(frozen=True)
class AgentContext:
    """Контекст одного прогона: уникальный request_id и пользовательский query."""

    request_id: RequestId
    query: str


class Agent:
    """Тонкий оркестратор: прогоняет source-стрим, отдаёт AgentEvent."""

    def __init__(
        self,
        source: StreamSource[AgentContext, AgentEvent],
    ) -> None:
        self._source = source

    def name(self) -> str:
        return "Agent"

    def stream(self, query: str) -> Iterator[AgentEvent]:
        """Прогнать агента; ленивый итератор AgentEvent. Не raise — события."""
        self._source.reset()

        yield from self._source.stream(
            AgentContext(
                request_id=new_request_id(),
                query=query,
            )
        )

    def invoke(self, query: str) -> str:
        """Прогнать агента до конца; вернуть текст последнего AnswerComplete."""
        last_answer = ""
        for event in self.stream(query):
            if isinstance(event, AnswerComplete):
                last_answer = event.content
        return last_answer
