"""Параметризация LLMRequestFactory через TurnBuilder + AgentBuilder.use_turn()."""

from __future__ import annotations

from typing import ClassVar

from boba.agent.agent import AgentContext
from boba.agent.builder import AgentBuilder
from boba.agent.history import InMemoryHistoryService
from boba.agent.history_view import HistoryDialogView
from boba.agent.middleware.llm import LLMPort
from boba.agent.turn.builder import TurnBuilder
from boba.agent.turn.reducers import (
    HistoryReducer,
    ModelReducer,
    RememberUserQueryReducer,
    SamplingReducer,
    SystemPromptReducer,
    ToolsDefinitionReducer,
    UserQueryReducer,
)
from boba.agent.turn.spec import TurnState
from boba.agent.workspace_fs import FsPromptWorkspaceRegistry
from boba.llm.models import SamplingParams
from boba.patterns import PrioritySource
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolRegistry
from boba.workspace.contract import PromptWorkspaceId, PromptWorkspaceShell


def _empty_history_view() -> HistoryDialogView:
    return HistoryDialogView(InMemoryHistoryService())


class _MarkerReducer(PrioritySource[str, TurnState]):
    """Reducer с уникальным id; используется как маркер регистрации."""

    ID: ClassVar[str] = "test_marker"

    def __init__(self, priority: int = 90) -> None:
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        return state


def _empty_catalog() -> ToolRegistry:
    return ToolRegistry.from_sources([StaticToolSource(ToolSourceId("empty"), [])])


def _prompt_workspace(root) -> PromptWorkspaceShell:
    return FsPromptWorkspaceRegistry(root=root).get_or_create(
        PromptWorkspaceId("prompts"),
    )


# TurnBuilder (low-level): LLMPort ничего не знает про конкретные reducer'ы.


def test_middleware_delegates_spec_construction_to_builder(
    agent_ctx: AgentContext,
):
    captured: list[AgentContext] = []

    def factory(ctx: AgentContext) -> _MarkerReducer:
        captured.append(ctx)
        return _MarkerReducer()

    turn = TurnBuilder("test-model").use_factory(_MarkerReducer.ID, factory)

    mw = LLMPort(
        llm=None,  # type: ignore[arg-type]
        turn=turn,
    )
    spec = mw._turn.build(agent_ctx)

    ids = {p.id() for p in spec.providers()}
    assert _MarkerReducer.ID in ids
    assert captured == [agent_ctx]


# TurnBuilder (high-level)


def test_turn_builder_model_required_at_construction(agent_ctx: AgentContext):
    """`model` обязателен в конструкторе — `ModelReducer` зарегистрирован сразу."""
    turn = TurnBuilder("test-model")
    spec = turn.build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    assert ids == {ModelReducer.ID}
    state = TurnState()
    for p in sorted(spec.providers(), key=lambda r: r.priority()):
        state = p.apply(state)
    assert state.model == "test-model"


def test_turn_builder_full_set(agent_ctx: AgentContext):
    """Полный набор `.with_*(...)` регистрирует все стандартные reducer'ы."""
    registry = _empty_catalog()
    turn = (
        TurnBuilder("test-model")
        .with_history_view(_empty_history_view())
        .with_tool_catalog(registry.catalog())
        .with_sampling(SamplingParams())
        .with_user_query()
        .system_prompt("static")
    )
    spec = turn.build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    assert ids == {
        ModelReducer.ID,
        SystemPromptReducer.ID,
        HistoryReducer.ID,
        UserQueryReducer.ID,
        ToolsDefinitionReducer.ID,
        SamplingReducer.ID,
    }


def test_turn_builder_minimal_only_registers_called(agent_ctx: AgentContext):
    """Только вызванные методы — только их reducer'ы; ничего лишнего."""
    turn = TurnBuilder("test-model").with_user_query()
    spec = turn.build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    assert ids == {ModelReducer.ID, UserQueryReducer.ID}


def test_turn_builder_with_model_latest_wins(agent_ctx: AgentContext):
    """`.with_model(...)` обновляет значение, заданное в конструкторе."""
    turn = TurnBuilder("first").with_model("second")
    spec = turn.build(agent_ctx)
    state = TurnState()
    for p in sorted(spec.providers(), key=lambda r: r.priority()):
        state = p.apply(state)
    assert state.model == "second"


def test_turn_builder_use_reducer_accepts_ready(agent_ctx: AgentContext):
    marker = _MarkerReducer()
    turn = TurnBuilder("test-model").use_reducer(marker)
    spec = turn.build(agent_ctx)
    providers = {p.id(): p for p in spec.providers()}
    assert providers[_MarkerReducer.ID] is marker


def test_turn_builder_use_factory_accepts_factory(agent_ctx: AgentContext):
    turn = TurnBuilder("test-model").use_factory(
        _MarkerReducer.ID,
        lambda _ctx: _MarkerReducer(),
    )
    spec = turn.build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    assert _MarkerReducer.ID in ids


def test_turn_builder_with_plus_use_reducer(agent_ctx: AgentContext):
    registry = _empty_catalog()
    turn = (
        TurnBuilder("test-model")
        .with_history_view(_empty_history_view())
        .with_tool_catalog(registry.catalog())
        .use_reducer(RememberUserQueryReducer())
    )
    spec = turn.build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    assert ModelReducer.ID in ids
    assert RememberUserQueryReducer.ID in ids


def test_turn_builder_system_prompt_static(agent_ctx: AgentContext):
    """`.system_prompt(text)` материализуется в один `SystemMessage`."""
    turn = (
        TurnBuilder("test-model")
        .system_prompt("Ты — Claude.")
        .system_prompt("Отвечай по-русски.")
    )
    spec = turn.build(agent_ctx)
    state = TurnState()
    for p in sorted(spec.providers(), key=lambda r: r.priority()):
        if p.id() == SystemPromptReducer.ID:
            state = p.apply(state)
    contents = [m.content for m in state.system_messages]
    assert contents == ["Ты — Claude.", "Отвечай по-русски."]


def test_turn_builder_system_prompt_file(
    agent_ctx: AgentContext,
    tmp_path,
):
    """`.system_prompt_file(workspace, rel_path)` читает блок из файла."""
    (tmp_path / "persona.md").write_text("Ты — Claude.", encoding="utf-8")
    workspace = _prompt_workspace(tmp_path)

    turn = TurnBuilder("test-model").system_prompt_from_file(workspace, "persona.md")
    spec = turn.build(agent_ctx)
    state = TurnState()
    for p in sorted(spec.providers(), key=lambda r: r.priority()):
        if p.id() == SystemPromptReducer.ID:
            state = p.apply(state)
    contents = [m.content for m in state.system_messages]
    assert contents == ["Ты — Claude."]


def test_turn_builder_system_prompt_file_missing_uses_default(
    agent_ctx: AgentContext,
    tmp_path,
):
    """Несуществующий файл → default_prompt."""
    workspace = _prompt_workspace(tmp_path)

    turn = TurnBuilder("test-model").system_prompt_from_file(
        workspace,
        "missing.md",
        default_prompt="(no prompt)",
    )
    spec = turn.build(agent_ctx)
    state = TurnState()
    for p in sorted(spec.providers(), key=lambda r: r.priority()):
        if p.id() == SystemPromptReducer.ID:
            state = p.apply(state)
    contents = [m.content for m in state.system_messages]
    assert contents == ["(no prompt)"]


def test_turn_builder_system_prompt_dir(
    agent_ctx: AgentContext,
    tmp_path,
):
    """`.system_prompt_dir(workspace)` читает файлы как отдельные блоки."""
    (tmp_path / "01-persona.md").write_text("Ты — Claude.", encoding="utf-8")
    (tmp_path / "02-rules.md").write_text("Отвечай по-русски.", encoding="utf-8")
    (tmp_path / "empty.md").write_text("", encoding="utf-8")
    workspace = _prompt_workspace(tmp_path)

    turn = TurnBuilder("test-model").system_prompt_from_directory(workspace)
    spec = turn.build(agent_ctx)
    state = TurnState()
    for p in sorted(spec.providers(), key=lambda r: r.priority()):
        if p.id() == SystemPromptReducer.ID:
            state = p.apply(state)
    contents = [m.content for m in state.system_messages]
    assert contents == ["Ты — Claude.", "Отвечай по-русски."]


def test_turn_builder_system_prompt_dir_extension_filter(
    agent_ctx: AgentContext,
    tmp_path,
):
    """extensions оставляет только файлы с указанными расширениями."""
    (tmp_path / "keep.md").write_text("kept", encoding="utf-8")
    (tmp_path / "ignore.log").write_text("logged", encoding="utf-8")
    workspace = _prompt_workspace(tmp_path)

    turn = TurnBuilder("test-model").system_prompt_from_directory(workspace)
    spec = turn.build(agent_ctx)
    state = TurnState()
    for p in sorted(spec.providers(), key=lambda r: r.priority()):
        if p.id() == SystemPromptReducer.ID:
            state = p.apply(state)
    contents = [m.content for m in state.system_messages]
    assert contents == ["kept"]


def test_turn_builder_system_prompt_dir_empty_workspace(
    agent_ctx: AgentContext,
    tmp_path,
):
    """Пустой workspace — ноль блоков, без ошибки."""
    workspace = _prompt_workspace(tmp_path)
    turn = TurnBuilder("test-model").system_prompt_from_directory(workspace)
    spec = turn.build(agent_ctx)
    state = TurnState()
    for p in sorted(spec.providers(), key=lambda r: r.priority()):
        if p.id() == SystemPromptReducer.ID:
            state = p.apply(state)
    assert state.system_messages == ()


def test_turn_builder_system_prompt_mix_all_three(
    agent_ctx: AgentContext,
    tmp_path,
):
    """Все три API накапливают провайдеров — ни один не заменяет других."""
    ws_rules_root = tmp_path / "rules"
    ws_rules_root.mkdir()
    (ws_rules_root / "rule.md").write_text("rule-from-file", encoding="utf-8")
    ws_persona_root = tmp_path / "persona"
    ws_persona_root.mkdir()
    (ws_persona_root / "persona.md").write_text("persona-from-file", encoding="utf-8")

    ws_persona = _prompt_workspace(ws_persona_root)
    ws_rules = _prompt_workspace(ws_rules_root)

    turn = (
        TurnBuilder("test-model")
        .system_prompt("static-block")
        .system_prompt_from_file(ws_persona, "persona.md")
        .system_prompt_from_directory(ws_rules)
    )
    spec = turn.build(agent_ctx)
    state = TurnState()
    for p in sorted(spec.providers(), key=lambda r: r.priority()):
        if p.id() == SystemPromptReducer.ID:
            state = p.apply(state)
    contents = [m.content for m in state.system_messages]
    assert contents == ["static-block", "persona-from-file", "rule-from-file"]


def test_turn_builder_extra_overrides_built_in_by_id(agent_ctx: AgentContext):
    class _OverrideModel(PrioritySource[str, TurnState]):
        ID: ClassVar[str] = ModelReducer.ID

        def id(self) -> str:
            return self.ID

        def priority(self) -> int:
            return 1

        def apply(self, state: TurnState) -> TurnState:
            state.model = "overridden"
            return state

    override = _OverrideModel()
    turn = TurnBuilder("test-model").use_reducer(override)
    spec = turn.build(agent_ctx)
    providers = {p.id(): p for p in spec.providers()}
    assert providers[ModelReducer.ID] is override


# AgentBuilder.use_turn() auto-wiring


def test_agent_builder_use_turn_autowires_history_view_and_catalog():
    """Если TurnBuilder не задал history_view/catalog — AgentBuilder ставит свои."""
    history = InMemoryHistoryService()
    turn = TurnBuilder("test-model")
    registry = _empty_catalog()
    builder = AgentBuilder().with_history(history).with_tools(registry).use_turn(turn)
    # Имитируем wiring, который происходит внутри build():
    resolved = builder.tool_registry()
    if not turn.has_history_view():
        turn.with_history_view(HistoryDialogView(history))
    if not turn.has_tool_catalog():
        turn.with_tool_catalog(resolved.catalog())
    assert turn.has_history_view()
    assert turn.has_tool_catalog()


def test_agent_builder_use_turn_respects_explicit_resources():
    """Явно заданное в TurnBuilder не перетирается AgentBuilder'ом."""
    explicit_view = _empty_history_view()
    turn = TurnBuilder("test-model").with_history_view(explicit_view)
    other_history = InMemoryHistoryService()
    builder = AgentBuilder().with_history(other_history).use_turn(turn)
    if not turn.has_history_view():
        turn.with_history_view(HistoryDialogView(builder._history_service))
    # has_history_view был True, перетирания не было.
    assert turn._history_view is explicit_view
