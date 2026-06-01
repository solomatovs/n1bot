"""TurnBuilder — bootstrap-recipe для TurnSpec.

`TurnBuilder` — fluent-фасад, описывающий *что* должно попасть в `LLMRequest`
каждой итерации. Каждый `.with_*(...)` отвечает за один reducer:
устанавливает ресурс и регистрирует соответствующую стадию. Reducer'ы,
которые пользователь не вызывал, не попадают в LLMRequest — например, без
`.with_tool_catalog(...)` запрос отправляется без tools.

Жизненный цикл:
- инстанс живёт пока жив `LLMPort` (per-process recipe);
- `build(ctx)` вызывается на каждой итерации и отдаёт свежий `TurnSpec`
  под текущий `AgentContext`.

Семантика регистрации — dict-by-id: повторная регистрация фабрики с тем же
`reducer_id` перезатирает прежнюю. Это даёт явный путь override:
`use_reducer(...)` с reducer'ом, чей `id()` совпадает с встроенным,
заменяет встроенный.

Ресурсы `HistoryDialogView` и `ToolCatalog` задаются явно через
`.with_history_view()` / `.with_tool_catalog()`. Caller создаёт
view над свежим `HistoryReader` и catalog'ом из `ToolRegistry`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Self, cast

from boba.agent.agent import AgentContext
from boba.agent.prompt import PromptId, PromptProvider
from boba.agent.prompt_providers import (
    DirectoryPromptProvider,
    FilePromptProvider,
    StaticPromptProvider,
)
from boba.agent.turn.history_view import HistoryDialogView
from boba.agent.turn.reducers import (
    HistoryReducer,
    ModelFromRequestReducer,
    RequestIdFromReducer,
    SamplingReducer,
    StreamModeReducer,
    SystemPromptReducer,
    ToolsDefinitionReducer,
    TurnReducer,
    UserQueryReducer,
)
from boba.agent.turn.spec import LLMRequest, LLMRequestFactory
from boba.agent.workspace_fs import FsWorkspaceShell
from boba.llm.models import SamplingParams
from boba.tools.framework import ToolCatalog
from boba.workspace.contract import WorkspaceId

__all__ = ["TurnBuilder", "TurnReducerFactory"]

TurnReducerFactory = Callable[[AgentContext], TurnReducer]


class TurnBuilder:
    """Fluent-описание следующего хода агента.

    `model` обязателен на этапе конструктора — без него запрос отправлять
    некуда. `ModelReducer` регистрируется сразу же; `with_model(...)` далее
    остаётся как явный override-путь.
    """

    def __init__(self, model: str) -> None:
        self._factories: dict[str, TurnReducerFactory] = {}
        self._system_prompt_providers: list[PromptProvider] = []
        self._history_view: HistoryDialogView | None = None
        self._tool_catalog: ToolCatalog | None = None
        self._model: str = model
        self._sampling: SamplingParams | None = None
        self._stream: bool = True
        self._factories[RequestIdFromReducer.ID] = self._make_request_id
        self._factories[ModelFromRequestReducer.ID] = self._make_model

    def with_model(self, model: str) -> Self:
        """Обновить LLM-модель. Перезаписывает значение, заданное в конструкторе."""
        self._model = model
        self._factories[ModelFromRequestReducer.ID] = self._make_model
        return self

    def with_sampling(self, sampling: SamplingParams) -> Self:
        """`SamplingParams` → `SamplingReducer`. Повторный вызов обновляет."""
        self._sampling = sampling
        self._factories[SamplingReducer.ID] = self._make_sampling
        return self

    def with_stream(self, stream: bool) -> Self:
        """Режим ответа LLM: True=стриминг дельт, False=один итоговый ответ."""
        self._stream = stream
        self._factories[StreamModeReducer.ID] = self._make_stream
        return self

    def with_history_view(self, view: HistoryDialogView) -> Self:
        """`HistoryDialogView` → `HistoryReducer`. Стандартно прокидывает Agent."""
        self._history_view = view
        self._factories[HistoryReducer.ID] = self._make_history
        return self

    def with_tool_catalog(self, catalog: ToolCatalog) -> Self:
        """`ToolCatalog` → `ToolsReducer`. Стандартно прокидывает Agent."""
        self._tool_catalog = catalog
        self._factories[ToolsDefinitionReducer.ID] = self._make_tools
        return self

    def with_user_query(self) -> Self:
        """Включить `UserQueryReducer` — префиксует UserMessage(ctx.query)."""
        self._factories[UserQueryReducer.ID] = self._make_user_query
        return self

    def system_prompt_from_providers(self, providers: Iterable[PromptProvider]) -> Self:
        """Полная замена списка system-prompt провайдеров + `SystemPromptReducer`.

        Для добавления отдельных источников — `.system_prompt(...)`,
        `.system_prompt_file(...)`, `.system_prompt_dir(...)`.
        """
        self._system_prompt_providers.extend(providers)
        self._factories[SystemPromptReducer.ID] = self._make_system_prompt
        return self

    def system_prompt(self, text: str, *, priority: int = 100) -> Self:
        """Добавить статичный system-prompt блок (отдельным `SystemMessage`)."""
        self.system_prompt_from_providers(
            [
                StaticPromptProvider(
                    PromptId(f"static_{len(self._system_prompt_providers)}"),
                    priority,
                    text,
                )
            ]
        )
        return self

    def system_prompt_from_file(
        self,
        file_path: Path,
        *,
        priority: int = 100,
        default_prompt: str = "",
    ) -> Self:
        """Добавить provider, читающий system-prompt из одного файла."""
        workspace = FsWorkspaceShell(WorkspaceId("prompt"), file_path.parent)
        rel_path = file_path.name
        self.system_prompt_from_providers(
            [
                FilePromptProvider(
                    PromptId(f"file_{len(self._system_prompt_providers)}_{rel_path}"),
                    priority,
                    workspace,
                    rel_path,
                    default_prompt=default_prompt,
                ),
            ]
        )
        return self

    def system_prompt_from_dir(
        self,
        dir_path: Path,
        *,
        priority: int = 100,
        extensions: tuple[str, ...] = (".md", ".txt"),
    ) -> Self:
        """Добавить provider, читающий файлы из директории как отдельные блоки."""
        workspace = FsWorkspaceShell(WorkspaceId("prompt"), dir_path)
        self.system_prompt_from_providers(
            [
                DirectoryPromptProvider(
                    PromptId(f"dir_{len(self._system_prompt_providers)}"),
                    priority,
                    workspace,
                    extensions=extensions,
                ),
            ]
        )
        return self

    def use_reducer(self, reducer: TurnReducer) -> Self:
        """Добавить готовый reducer. Повтор по `reducer.id()` перезатирает.

        Это явный путь override встроенного: зарегистрируй свой reducer
        с тем же `id()` — он заменит встроенный.
        """
        self._factories[reducer.id()] = lambda _ctx: reducer
        return self

    def use_factory(
        self,
        reducer_id: str,
        factory: TurnReducerFactory,
    ) -> Self:
        """Добавить фабрику `(ctx) -> reducer` под явный id.

        Для случаев, когда reducer зависит от `AgentContext`. Повтор по
        `reducer_id` перезатирает.
        """
        self._factories[reducer_id] = factory
        return self

    def build(self, ctx: AgentContext) -> LLMRequest:
        """Прогнать все фабрики над `ctx`, заполнить spec, отдать LLMRequest."""
        spec = LLMRequestFactory()

        for factory in self._factories.values():
            spec.register(factory(ctx))

        return spec.build()

    # --- фабрики reducer'ов (читают live state self.* и ctx) -----------

    def _make_request_id(self, ctx: AgentContext) -> RequestIdFromReducer:
        return RequestIdFromReducer(ctx.request_id)

    def _make_model(self, _ctx: AgentContext) -> ModelFromRequestReducer:
        return ModelFromRequestReducer(self._model)

    def _make_sampling(self, _ctx: AgentContext) -> SamplingReducer:
        return SamplingReducer(self._sampling)

    def _make_stream(self, _ctx: AgentContext) -> StreamModeReducer:
        return StreamModeReducer(self._stream)

    def _make_history(self, _ctx: AgentContext) -> HistoryReducer:
        return HistoryReducer(cast("HistoryDialogView", self._history_view))

    def _make_tools(self, _ctx: AgentContext) -> ToolsDefinitionReducer:
        return ToolsDefinitionReducer(cast("ToolCatalog", self._tool_catalog))

    def _make_user_query(self, ctx: AgentContext) -> UserQueryReducer:
        return UserQueryReducer(ctx.query)

    def _make_system_prompt(self, _ctx: AgentContext) -> SystemPromptReducer:
        return SystemPromptReducer(list(self._system_prompt_providers))
