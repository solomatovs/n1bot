"""TurnSpecBuilder — bootstrap-recipe для TurnSpec.

`TurnSpec` сам по себе — per-call fold-фабрика: его наполняют reducer'ами и
сразу строят `LLMRequest`. Состав reducer'ов же — *bootstrap*-уровень: он
известен один раз при сборке агента и не меняется между итерациями.
`TurnSpecBuilder` владеет этим составом — списком фабрик
`(AgentContext) -> TurnReducer`, — и под каждый `AgentContext` отдаёт
свежий, наполненный `TurnSpec`.

Разделение ответственности:
- `TurnSpec` — *что* собрать (fold по reducer'ам, валидация результата).
- `TurnSpecBuilder` — *из чего* собрать (декларация состава, late-binding
  ресурсов через closure).
- `LLMInvokeMiddleware` — *когда* (per-call: build → invoke → events).

Состав `TurnSpec` полностью определяется на bootstrap-уровне через
`AgentBuilder.use_turn_reducer(...)` / `use_default_turn_reducers()`.
Middleware про конкретные reducer'ы ничего не знает — это даёт возможность
добавлять/удалять стадии без правок middleware.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Self

from boba.agent.orchestrator import AgentContext
from boba.agent.turn.reducers import TurnReducer
from boba.agent.turn.spec import TurnSpec
from boba.patterns import PrioritySource

__all__ = ["TurnReducerFactory", "TurnSpecBuilder"]

TurnReducerFactory = Callable[[AgentContext], TurnReducer]


class TurnSpecBuilder:
    """Накапливает reducer-фабрики; под каждый AgentContext отдаёт свежий TurnSpec.

    Фабрика принимает текущий `AgentContext` и возвращает `TurnReducer`.
    Готовый `TurnReducer` оборачивается в trivial-фабрику автоматически —
    `add()` принимает оба варианта.

    Жизненный цикл:
    - инстанс живёт пока жив `AgentBuilder` (per-process recipe);
    - `build(ctx)` вызывается на каждой итерации (per-call materialization).

    Семантика регистрации — `FoldFactory.register`: dict-by-id, повторная
    регистрация с тем же `reducer.id()` перезатирает. Это даёт явный путь
    переопределения дефолта: зарегистрируй свой reducer с тем же id.
    """

    def __init__(self) -> None:
        self._factories: list[TurnReducerFactory] = []

    def add(
        self,
        reducer_or_factory: TurnReducer | TurnReducerFactory,
    ) -> Self:
        """Добавить стадию: готовый reducer или фабрику `(ctx) -> reducer`."""
        factory: TurnReducerFactory
        if isinstance(reducer_or_factory, PrioritySource):
            ready: TurnReducer = reducer_or_factory
            factory = lambda _ctx: ready  # noqa: E731
        else:
            factory = reducer_or_factory
        self._factories.append(factory)
        return self

    def is_empty(self) -> bool:
        """True, если ни одной фабрики ещё не зарегистрировано."""
        return not self._factories

    def build(self, ctx: AgentContext) -> TurnSpec:
        """Свежий TurnSpec под `ctx`: вызвать все фабрики, зарегистрировать в spec."""
        spec = TurnSpec()
        for f in self._factories:
            spec.register(f(ctx))
        return spec
