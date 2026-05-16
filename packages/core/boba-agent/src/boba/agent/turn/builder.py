"""TurnSpecBuilder + TurnBuilder — bootstrap-recipe для TurnSpec.

Два уровня абстракции:

- `TurnSpecBuilder` — low-level: голый список factory'ев
  `(AgentContext) -> TurnReducer`. Под каждый `AgentContext` отдаёт
  свежий `TurnSpec`. Используется внутри `LLMPort`.

- `TurnBuilder` — high-level fluent facade: пользователь задаёт
  `model`/`sampling`/`prompts`/`messages`/`tool_catalog` и набор
  reducer'ов; `build_spec_builder()` собирает `TurnSpecBuilder` с
  late-bound замыканиями на эти ресурсы. Это позволяет конфигурировать
  Turn отдельно от Agent и тестировать изолированно.

Жизненный цикл:
- `TurnBuilder` — describe-only объект, живёт пока его не «материализуют».
- `TurnSpecBuilder` — recipe; живёт пока жив `LLMPort` (per-process).
- `TurnSpec` — per-call: вызывается на каждой итерации.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Self

from boba.agent.agent import AgentContext
from boba.agent.messages import MessageReader
from boba.agent.prompt import PromptProvider
from boba.agent.turn.reducers import (
    HistoryReducer,
    ModelReducer,
    SamplingReducer,
    SystemPromptReducer,
    ToolsReducer,
    TurnReducer,
)
from boba.agent.turn.spec import TurnSpec
from boba.llm.models import SamplingParams
from boba.patterns import PrioritySource
from boba.tools.framework import ToolCatalog

__all__ = ["TurnBuilder", "TurnReducerFactory", "TurnSpecBuilder"]

TurnReducerFactory = Callable[[AgentContext], TurnReducer]


class TurnSpecBuilder:
    """Накапливает reducer-фабрики; под каждый AgentContext отдаёт свежий TurnSpec.

    Фабрика принимает текущий `AgentContext` и возвращает `TurnReducer`.
    Готовый `TurnReducer` оборачивается в trivial-фабрику автоматически —
    `add()` принимает оба варианта.

    Жизненный цикл:
    - инстанс живёт пока жив `LLMPort` (per-process recipe);
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
        spec = TurnSpec(ctx.request_id)
        for f in self._factories:
            spec.register(f(ctx))
        return spec


class TurnBuilder:
    """Fluent-описание следующего хода агента.

    Самостоятельный bootstrap: описывает *что* должно попасть в `LLMRequest`
    каждой итерации (модель/sampling/системный prompt/историю/каталог tools/
    дополнительные reducer'ы) — независимо от `AgentBuilder`. Можно
    собирать и тестировать изолированно.

    Ресурсы `MessageReader` (для `HistoryReducer`) и `ToolCatalog` (для
    `ToolsReducer`) задаются либо явно через `.with_messages()` /
    `.with_tool_catalog()`, либо прокидываются `AgentBuilder.use_turn()`
    из собственных полей. Явно заданное в `TurnBuilder` не перетирается.
    """

    def __init__(self) -> None:
        self._model: str | None = None
        self._sampling: SamplingParams | None = None
        self._prompt_providers: list[PromptProvider] = []
        self._messages: MessageReader | None = None
        self._tool_catalog: ToolCatalog | None = None
        self._extras: list[TurnReducer | TurnReducerFactory] = []
        self._use_defaults: bool = False

    def with_model(self, model: str) -> Self:
        """LLM-модель (обязательна, если используется `use_default_reducers`)."""
        self._model = model
        return self

    def with_sampling(self, sampling: SamplingParams) -> Self:
        """`SamplingParams` для `SamplingReducer`. Опционально."""
        self._sampling = sampling
        return self

    def with_prompts(self, providers: Iterable[PromptProvider]) -> Self:
        """Провайдеры system-prompt блоков для `SystemPromptReducer`."""
        self._prompt_providers = list(providers)
        return self

    def with_messages(self, messages: MessageReader) -> Self:
        """`MessageReader` для `HistoryReducer`. Стандартно прокидывает Agent."""
        self._messages = messages
        return self

    def with_tool_catalog(self, catalog: ToolCatalog) -> Self:
        """`ToolCatalog` для `ToolsReducer`. Стандартно прокидывает Agent."""
        self._tool_catalog = catalog
        return self

    def use_reducer(
        self,
        reducer_or_factory: TurnReducer | TurnReducerFactory,
    ) -> Self:
        """Добавить дополнительный reducer (или фабрику).

        Reducer с тем же `id()` перезатрёт ранее зарегистрированный —
        в том числе из дефолтного набора. Это явный путь override.
        """
        self._extras.append(reducer_or_factory)
        return self

    def use_default_reducers(self) -> Self:
        """Включить дефолтный набор reducer'ов.

        Состав: `model` / `system` / `history` / `tools` / `sampling`.
        Зависимости (`model`, `messages`, `tool_catalog`, `prompts`,
        `sampling`) проверяются в `build_spec_builder()` — порядок
        fluent-вызовов не важен.
        """
        self._use_defaults = True
        return self

    def has_messages(self) -> bool:
        """True, если пользователь уже задал `MessageReader` явно."""
        return self._messages is not None

    def has_tool_catalog(self) -> bool:
        """True, если пользователь уже задал `ToolCatalog` явно."""
        return self._tool_catalog is not None

    def build_spec_builder(self) -> TurnSpecBuilder:
        """Финализация: late-bind ресурсы → собранный `TurnSpecBuilder`."""
        if not self._use_defaults and not self._extras:
            msg = (
                "TurnBuilder.build_spec_builder: ни одного reducer'а не "
                "задано. Вызови .use_default_reducers() или .use_reducer(...)."
            )
            raise ValueError(msg)

        spec_builder = TurnSpecBuilder()

        if self._use_defaults:
            if self._model is None:
                msg = (
                    "TurnBuilder.use_default_reducers: .with_model(...) обязателен."
                )
                raise ValueError(msg)
            if self._messages is None:
                msg = (
                    "TurnBuilder.use_default_reducers: .with_messages(...) "
                    "обязателен (для HistoryReducer). Обычно прокидывает "
                    "AgentBuilder.use_turn()."
                )
                raise ValueError(msg)
            if self._tool_catalog is None:
                msg = (
                    "TurnBuilder.use_default_reducers: .with_tool_catalog(...) "
                    "обязателен (для ToolsReducer). Обычно прокидывает "
                    "AgentBuilder.use_turn()."
                )
                raise ValueError(msg)

            model = self._model
            messages = self._messages
            catalog = self._tool_catalog
            prompts = list(self._prompt_providers)
            sampling = self._sampling

            spec_builder.add(lambda _ctx: ModelReducer(model))
            spec_builder.add(lambda _ctx: SystemPromptReducer(prompts))
            spec_builder.add(lambda _ctx: HistoryReducer(messages))
            spec_builder.add(lambda _ctx: ToolsReducer(catalog))
            spec_builder.add(lambda _ctx: SamplingReducer(sampling))

        for extra in self._extras:
            spec_builder.add(extra)

        return spec_builder
