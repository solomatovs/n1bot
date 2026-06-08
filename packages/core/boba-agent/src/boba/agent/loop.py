"""Агент-специфичный цикл: дренаж круга до StopIteration + stop_if-завершение."""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent.agent import AgentContext
from boba.agent.events import AgentEvent
from boba.patterns import Specification, StreamSource


class AgentLoop(StreamSource[AgentContext, AgentEvent]):
    """Циклический прогон source до срабатывания stop_if (проверка после yield).

    Отличие от generic StreamSourceLoop — способ завершения. При срабатывании
    stop_if цикл НЕ обрывает inner-генератор (`return` посреди `for`), а помечает
    остановку и дочитывает текущий круг до его естественного StopIteration.

    Зачем: `return` посреди `for` бросает недочитанный inner-генератор, и Python
    закрывает его через GeneratorExit — терминал воспринимает это как отмену
    (on_request_cancel), хотя генерация завершилась штатно. Дренаж же доводит
    inner до StopIteration, терминал закрывается нормальным путём
    (on_request_end). GeneratorExit остаётся только под реальную отмену
    (Ctrl-C / таймаут), а не под обычный финал круга.

    Семантика stop_if при этом не меняется: те же спеки, тот же момент проверки.
    """

    def __init__(
        self,
        source: StreamSource[AgentContext, AgentEvent],
        stop_if: Specification[tuple[AgentContext, AgentEvent]],
    ) -> None:
        self._source = source
        self._stop_if = stop_if

    def name(self) -> str:
        return "AgentLoop(" + self._source.name() + ")"

    def reset(self) -> None:
        self._source.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        while True:
            stop = False
            for event in self._source.stream(ctx):
                yield event

                if self._stop_if.check((ctx, event)):
                    # помечаем и дочитываем круг до StopIteration
                    # чтобы терминал закрылся штатно.
                    stop = True

            if stop:
                return


__all__ = ["AgentLoop"]
