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

from boba.agent.events import AgentEvent, AnswerMessage
from boba.llm.models import RequestId, new_request_id
from boba.patterns import StreamSource


@dataclass(frozen=True)
class AgentContext:
    """
    Контекст одного прогона:
        request_id      - новый id запроса
        query           - запрос пользователя
        DI Container    - DI контейнер
    """

    request_id: RequestId
    query: str
    container: Container


class Agent:
    """
    Тонкий оркестратор:
        stream - генерирует AgentEvent в процессе работы
        invoke - ждет итоговый ответ модели
    """

    def __init__(
        self,
        source: StreamSource[AgentContext, AgentEvent],
        container: Container,
    ) -> None:
        self._source = source
        self._container = container

    @property
    def container(self) -> Container:
        """DI-контейнер агента. Тесты/UI могут вытянуть из него любые
        зарегистрированные сервисы (HistoryReader, HistoryWriter, ...)."""
        return self._container

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
        """Прогнать агента до конца; вернуть текст последнего AnswerMessage."""
        last_answer = ""
        for event in self.stream(query):
            if isinstance(event, AnswerMessage):
                last_answer = event.content
        return last_answer

    def close(self) -> None:
        """Освободить ресурсы DI-контейнера (APP-scope teardown'ы)."""
        self._container.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
