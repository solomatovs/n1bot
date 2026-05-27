from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from boba.agent.agent import AgentContext
from boba.agent.errors import MaxIterationsExceededError
from boba.agent.events import (
    AgentEvent,
    GenerationCompleted,
    IterationStarted,
    TerminalEvent,
)
from boba.llm.events import FinishReason
from boba.patterns import Specification, StreamSource


class IterationCounterConfig(BaseModel):
    """Конфиг bootstrap-параметров `IterationCounterMiddleware`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_iterations: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок числа итераций агента в одной сессии.",
    )


class IterationCounterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Счётчик итераций
    Прерывает агентский цикл по достижении max_iterations
    через событие MaxIterationsExceededError"""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        cfg: IterationCounterConfig,
    ) -> None:
        self._inner = inner
        self._max_iterations = cfg.max_iterations
        self._iteration = 0

    def name(self) -> str:
        return "IterationCounter"

    def reset(self) -> None:
        self._iteration = 0
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        self._iteration += 1

        if self._iteration > self._max_iterations:
            raise MaxIterationsExceededError(
                f"Исчерпан лимит итераций цикла агента: {self._iteration} > "
                f"{self._max_iterations}. Финальный ответ не получен.",
                limit=self._max_iterations,
                iteration=self._iteration,
            )

        yield IterationStarted(
            request_id=ctx.request_id,
            iteration_count=self._iteration,
            max_iterations=self._max_iterations,
        )

        yield from self._inner.stream(ctx)


class StopIfReasonStop(Specification[tuple[AgentContext, AgentEvent]]):
    """
    Стопает цикл, если модель отдала финальный текстовый ответ.

    Срабатывает на `GenerationCompleted`, у которого:
        - `finish_reason == STOP` (модель сама решила, что закончила)
        - в `message.tool_calls` пусто (модель не зовёт инструменты)

    Если tool_calls есть — `finish_reason=stop` игнорируется (мелкие модели
    часто отдают `stop` вместо `tool_calls`, нам важен факт вызова tool'а,
    а не подменный finish_reason).

    Это «нормальный» путь завершения круга вопрос-ответ.
    """

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate

        if not isinstance(event, GenerationCompleted):
            return False

        # тут лайфхак!
        # не все модели шлют finish_reason: tool_calls
        # многие присылают tool_call но в конце все равно отдают finish_reason=stop
        # именно такую ситуацию я здесь обрабатываю
        # она означает, что агентский цикл нельзя завершать
        # так как модель хотела вызвать tool, хоть и сообщила stop
        if event.message.tool_calls:
            return False

        return event.finish_reason is FinishReason.STOP


class StopIfLengthReached(Specification[tuple[AgentContext, AgentEvent]]):
    """
    Стопает цикл, если генерация упёрлась в лимит токенов.

    Срабатывает на `GenerationCompleted` с `finish_reason == LENGTH` независимо
    от tool_calls. Tool-call, обрезанный по длине, выполнять опасно — args
    могут оказаться невалидным JSON или с потерей хвоста; лучше остановить
    цикл и дать вышестоящему коду решить, что делать (поднять max_tokens,
    попросить «continue», вернуть пользователю что есть).

    Если в твоём приложении нужна авто-продолжающаяся стратегия — не подключай
    этот spec в `stop_if`, и пусть цикл крутится дальше (но тогда нужен
    отдельный механизм, который добавит «continue» в историю).
    """

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate

        if not isinstance(event, GenerationCompleted):
            return False

        return event.finish_reason is FinishReason.LENGTH


class StopIfContentFilter(Specification[tuple[AgentContext, AgentEvent]]):
    """
    Стопает цикл, если провайдерский фильтр заблокировал генерацию.

    Срабатывает на `GenerationCompleted` с `finish_reason == CONTENT_FILTER`.
    Повторять с тем же промптом смысла нет — фильтр сработает снова.
    Решение об альтернативном промпте или эскалации пользователю — за
    вышестоящим слоем.
    """

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate

        if not isinstance(event, GenerationCompleted):
            return False

        return event.finish_reason is FinishReason.CONTENT_FILTER


class StopOnAnyFailure(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает цикл при любом TerminalEvent."""

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate

        return isinstance(event, TerminalEvent)
