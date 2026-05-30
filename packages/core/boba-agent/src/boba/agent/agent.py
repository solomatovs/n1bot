"""Оркестратор агента: stream — поток событий, invoke — текст итогового ответа.

`Agent` — тонкий wrapper над `StreamSource[AgentContext, AgentEvent]`,
собранным `AgentBuilder.build(terminal)`. Никакими shared-ресурсами не
владеет — history, tool registry, llm и turn принадлежат caller'у.

`AgentContext` несёт `request_id` и `query` — этого достаточно, чтобы
mandatory middleware могли работать. Tool-DI поднимает request-scope
у себя (`DishkaTool.execute`), `AgentContext` про tool-container ничего
не знает.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from boba.agent.events import AgentEvent, AnswerMessage
from boba.llm.models import RequestId, new_request_id
from boba.patterns import StreamSource


@dataclass(frozen=True)
class AgentContext:
    """Контекст одного прогона: новый `request_id` + текст пользовательского запроса."""

    request_id: RequestId
    query: str


class Agent:
    """Тонкий оркестратор: `stream` — поток событий, `invoke` — итоговый ответ."""

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
        """Прогнать агента до конца; вернуть текст последнего AnswerMessage."""
        last_answer = ""
        for event in self.stream(query):
            if isinstance(event, AnswerMessage):
                last_answer = event.content
        return last_answer
