"""Оркестратор агента: stream — поток событий, invoke — текст итогового ответа.

Agent владеет Dishka `Container` — общим DI-реестром всех служб агента.
Container строится `AgentBuilder.build()` и передаётся сюда. Жизненный
цикл Container'а совпадает с жизненным циклом Agent'а:

- `Scope.APP` инстансы создаются лениво при первом резолве и живут до
  `Agent.close()` (тогда `Container.close()` запускает их teardown'ы).
- `Scope.REQUEST` инстансы создаются при `tool.invoke()` (внутри
  `with container(): ...`), уничтожаются по выходу из request-scope.

`Agent` поддерживает context manager protocol — используй `with`:

    with AgentBuilder()...build() as agent:
        for event in agent.stream(query):
            ...
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Self

from dishka import Container

from boba.agent.events import AgentEvent, AnswerComplete
from boba.llm.models import RequestId, new_request_id
from boba.patterns import StreamSource


@dataclass(frozen=True)
class AgentContext:
    """Контекст одного прогона: request_id, query, DI Container.

    `container` опционален для совместимости с тестами/middleware'ами,
    которые не зависят от DI. В рабочем агенте Container всегда есть —
    `AgentBuilder.build()` его проставляет, `Agent.stream(...)` передаёт
    каждому tick'у через AgentContext.
    """

    request_id: RequestId
    query: str
    container: Container | None = None


class Agent:
    """Тонкий оркестратор: прогоняет source-стрим, отдаёт AgentEvent.

    Владеет `Container` — общим DI-реестром всех служб агента. Закрывает
    его на `close()` / выходе из `with`-блока.
    """

    def __init__(
        self,
        source: StreamSource[AgentContext, AgentEvent],
        container: Container,
    ) -> None:
        self._source = source
        self._container = container

    def name(self) -> str:
        return "Agent"

    def stream(self, query: str) -> Iterator[AgentEvent]:
        """Прогнать агента; ленивый итератор AgentEvent. Не raise — события."""
        self._source.reset()

        yield from self._source.stream(
            AgentContext(
                request_id=new_request_id(),
                query=query,
                container=self._container,
            )
        )

    def invoke(self, query: str) -> str:
        """Прогнать агента до конца; вернуть текст последнего AnswerComplete."""
        last_answer = ""
        for event in self.stream(query):
            if isinstance(event, AnswerComplete):
                last_answer = event.content
        return last_answer

    def close(self) -> None:
        """Освободить ресурсы DI-контейнера (APP-scope teardown'ы)."""
        self._container.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
