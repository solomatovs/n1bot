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
from pathlib import Path
from typing import Self, cast

from boba.agent.agent import AgentContext
from boba.agent.messages import MessageReader
from boba.agent.prompt import PromptId, PromptProvider, StaticPromptProvider
from boba.agent.prompt_providers import DirectoryPromptProvider
from boba.agent.turn.reducers import (
    HistoryReducer,
    ModelReducer,
    SamplingReducer,
    SystemPromptReducer,
    ToolsDefinitionReducer,
    TurnReducer,
    UserQueryReducer,
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
    каждой итерации. Каждый `.with_*(...)` отвечает за один reducer:
    устанавливает ресурс и регистрирует соответствующую стадию в
    `TurnSpecBuilder`. Повторный вызов того же метода обновляет ресурс
    (без дублирования фабрики). Reducer'ы, которые пользователь не вызывал,
    не попадают в LLMRequest — например, без `.with_tool_catalog(...)`
    запрос отправляется без tools.

    Ресурсы `MessageReader` и `ToolCatalog` задаются либо явно через
    `.with_messages()` / `.with_tool_catalog()`, либо прокидываются
    `AgentBuilder.use_turn()` (он уважает явно заданное).
    """

    def __init__(self) -> None:
        self._spec_builder = TurnSpecBuilder()
        self._prompt_providers: list[PromptProvider] = []
        self._messages: MessageReader | None = None
        self._tool_catalog: ToolCatalog | None = None
        self._model: str | None = None
        self._sampling: SamplingParams | None = None
        self._registered: set[str] = set()

    def _ensure(self, reducer_id: str, factory: TurnReducerFactory) -> None:
        """Идемпотентная регистрация фабрики reducer'а по его id."""
        if reducer_id in self._registered:
            return
        self._spec_builder.add(factory)
        self._registered.add(reducer_id)

    def with_model(self, model: str) -> Self:
        """LLM-модель → `ModelReducer`. Повторный вызов обновляет значение."""
        self._model = model
        self._ensure(ModelReducer.ID, self._make_model)
        return self

    def with_sampling(self, sampling: SamplingParams) -> Self:
        """`SamplingParams` → `SamplingReducer`. Повторный вызов обновляет."""
        self._sampling = sampling
        self._ensure(SamplingReducer.ID, self._make_sampling)
        return self

    def with_messages(self, messages: MessageReader) -> Self:
        """`MessageReader` → `HistoryReducer`. Стандартно прокидывает Agent."""
        self._messages = messages
        self._ensure(HistoryReducer.ID, self._make_history)
        return self

    def with_tool_catalog(self, catalog: ToolCatalog) -> Self:
        """`ToolCatalog` → `ToolsReducer`. Стандартно прокидывает Agent."""
        self._tool_catalog = catalog
        self._ensure(ToolsDefinitionReducer.ID, self._make_tools)
        return self

    def with_user_query(self) -> Self:
        """Включить `UserQueryReducer` — префиксует UserMessage(ctx.query)."""
        self._ensure(UserQueryReducer.ID, self._make_user_query)
        return self

    def with_prompts(self, providers: Iterable[PromptProvider]) -> Self:
        """Полная замена списка system-prompt провайдеров + `SystemPromptReducer`.

        Для добавления отдельных источников — `.system_prompt(...)` и
        `.system_prompt_from_dir(...)`.
        """
        self._prompt_providers = list(providers)
        self._ensure(SystemPromptReducer.ID, self._make_system_prompt)
        return self

    def system_prompt(self, text: str, *, priority: int = 100) -> Self:
        """Добавить статичный system-prompt блок (отдельным `SystemMessage`)."""
        pid = PromptId(f"static_{len(self._prompt_providers)}")
        self._prompt_providers.append(StaticPromptProvider(pid, priority, text))
        self._ensure(SystemPromptReducer.ID, self._make_system_prompt)
        return self

    def system_prompt_from_dir(
        self,
        directory: Path,
        *,
        priority: int = 100,
        glob: str = "*",
    ) -> Self:
        """Добавить provider, читающий файлы из директории как отдельные блоки."""
        pid = PromptId(f"dir_{len(self._prompt_providers)}_{directory.name}")
        self._prompt_providers.append(
            DirectoryPromptProvider(pid, priority, directory, glob=glob),
        )
        self._ensure(SystemPromptReducer.ID, self._make_system_prompt)
        return self

    def use_reducer(
        self,
        reducer_or_factory: TurnReducer | TurnReducerFactory,
    ) -> Self:
        """Добавить произвольный reducer (или фабрику от `AgentContext`).

        Reducer с уже зарегистрированным `id()` перезатрёт ранее
        зарегистрированный — это явный путь override встроенного.
        """
        self._spec_builder.add(reducer_or_factory)
        return self

    def has_messages(self) -> bool:
        """True, если пользователь уже задал `MessageReader` явно."""
        return self._messages is not None

    def has_tool_catalog(self) -> bool:
        """True, если пользователь уже задал `ToolCatalog` явно."""
        return self._tool_catalog is not None

    def build_spec_builder(self) -> TurnSpecBuilder:
        """Финализация — вернуть накопленный `TurnSpecBuilder`."""
        if self._spec_builder.is_empty():
            msg = (
                "TurnBuilder.build_spec_builder: ни одного reducer'а не "
                "задано. Вызови `.with_model(...)`/`.with_*(...)`-методы "
                "и/или `.use_reducer(...)`."
            )
            raise ValueError(msg)
        return self._spec_builder

    # --- фабрики reducer'ов (читают live state self.*) ------------------

    def _make_model(self, _ctx: AgentContext) -> ModelReducer:
        # инвариант: _ensure регистрирует фабрику только после `with_model`,
        # который выставляет `self._model`; cast уговаривает type-checker.
        return ModelReducer(cast("str", self._model))

    def _make_sampling(self, _ctx: AgentContext) -> SamplingReducer:
        return SamplingReducer(self._sampling)

    def _make_history(self, _ctx: AgentContext) -> HistoryReducer:
        return HistoryReducer(cast("MessageReader", self._messages))

    def _make_tools(self, _ctx: AgentContext) -> ToolsDefinitionReducer:
        return ToolsDefinitionReducer(cast("ToolCatalog", self._tool_catalog))

    def _make_user_query(self, ctx: AgentContext) -> UserQueryReducer:
        return UserQueryReducer(ctx.query)

    def _make_system_prompt(self, _ctx: AgentContext) -> SystemPromptReducer:
        return SystemPromptReducer(list(self._prompt_providers))
