"""Параметризация LLMRequest через TurnBuilder."""

from __future__ import annotations

from typing import ClassVar

from boba.agent.agent import AgentContext
from boba.agent.history import InMemoryHistoryService
from boba.agent.middleware.llm import LLMPort
from boba.agent.turn.builder import TurnBuilder
from boba.agent.turn.history_view import AllHistoryDialogView, HistoryDialogView
from boba.agent.turn.reducers import (
    HistoryReducer,
    ModelFromRequestReducer,
    RememberUserQueryReducer,
    RequestIdFromReducer,
    SamplingReducer,
    SystemPromptReducer,
    ToolsDefinitionReducer,
    UserQueryReducer,
)
from boba.agent.turn.spec import TurnState
from boba.llm.models import SamplingParams
from boba.patterns import PrioritySource
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolRegistry


def _empty_history_view() -> HistoryDialogView:
    return AllHistoryDialogView(InMemoryHistoryService())


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
    return ToolRegistry(sources=[StaticToolSource(ToolSourceId("empty"), [])])


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
    request = mw._turn.build(agent_ctx)

    assert request.model == "test-model"
    assert captured == [agent_ctx]


# TurnBuilder (high-level)


def test_turn_builder_model_required_at_construction(agent_ctx: AgentContext):
    """model обязателен в конструкторе — ModelReducer и RequestIdReducer сразу."""
    turn = TurnBuilder("test-model")
    assert set(turn._factories.keys()) == {
        ModelFromRequestReducer.ID,
        RequestIdFromReducer.ID,
    }
    request = turn.build(agent_ctx)
    assert request.model == "test-model"
    assert request.request_id == agent_ctx.request_id


def test_turn_builder_full_set(agent_ctx: AgentContext):
    """Полный набор .with_*(...) регистрирует все стандартные reducer'ы."""
    registry = _empty_catalog()
    turn = (
        TurnBuilder("test-model")
        .with_history_view(_empty_history_view())
        .with_tool_catalog(registry.catalog())
        .with_sampling(SamplingParams())
        .with_user_query()
        .system_prompt("static")
    )
    assert set(turn._factories.keys()) == {
        RequestIdFromReducer.ID,
        ModelFromRequestReducer.ID,
        SystemPromptReducer.ID,
        HistoryReducer.ID,
        UserQueryReducer.ID,
        ToolsDefinitionReducer.ID,
        SamplingReducer.ID,
    }


def test_turn_builder_minimal_only_registers_called(agent_ctx: AgentContext):
    """Только вызванные методы (+ обязательные request_id/model) — ничего лишнего."""
    turn = TurnBuilder("test-model").with_user_query()
    assert set(turn._factories.keys()) == {
        RequestIdFromReducer.ID,
        ModelFromRequestReducer.ID,
        UserQueryReducer.ID,
    }


def test_turn_builder_with_model_latest_wins(agent_ctx: AgentContext):
    """.with_model(...) обновляет значение, заданное в конструкторе."""
    turn = TurnBuilder("first").with_model("second")
    request = turn.build(agent_ctx)
    assert request.model == "second"


def test_turn_builder_use_reducer_accepts_ready(agent_ctx: AgentContext):
    """use_reducer(ready) регистрирует фабрику, возвращающую тот же инстанс."""
    marker = _MarkerReducer()
    turn = TurnBuilder("test-model").use_reducer(marker)
    assert turn._factories[_MarkerReducer.ID](agent_ctx) is marker


def test_turn_builder_use_factory_accepts_factory(agent_ctx: AgentContext):
    """use_factory(id, fn) регистрирует фабрику под явный id."""
    turn = TurnBuilder("test-model").use_factory(
        _MarkerReducer.ID,
        lambda _ctx: _MarkerReducer(),
    )
    assert _MarkerReducer.ID in turn._factories


def test_turn_builder_with_plus_use_reducer(agent_ctx: AgentContext):
    registry = _empty_catalog()
    turn = (
        TurnBuilder("test-model")
        .with_history_view(_empty_history_view())
        .with_tool_catalog(registry.catalog())
        .use_reducer(RememberUserQueryReducer())
    )
    ids = set(turn._factories.keys())
    assert ModelFromRequestReducer.ID in ids
    assert RememberUserQueryReducer.ID in ids


def test_turn_builder_system_prompt_static(agent_ctx: AgentContext):
    """.system_prompt(text) материализуется в один SystemMessage."""
    turn = (
        TurnBuilder("test-model")
        .system_prompt("Ты — Claude.")
        .system_prompt("Отвечай по-русски.")
    )
    request = turn.build(agent_ctx)
    contents = [m.content for m in request.system_messages]
    assert contents == ["Ты — Claude.", "Отвечай по-русски."]


def test_turn_builder_system_prompt_file(
    agent_ctx: AgentContext,
    tmp_path,
):
    """.system_prompt_from_file(file_path) читает блок из файла."""
    (tmp_path / "persona.md").write_text("Ты — Claude.", encoding="utf-8")

    turn = TurnBuilder("test-model").system_prompt_from_file(tmp_path / "persona.md")
    request = turn.build(agent_ctx)
    contents = [m.content for m in request.system_messages]
    assert contents == ["Ты — Claude."]


def test_turn_builder_system_prompt_file_missing_uses_default(
    agent_ctx: AgentContext,
    tmp_path,
):
    """Несуществующий файл -> default_prompt."""
    turn = TurnBuilder("test-model").system_prompt_from_file(
        tmp_path / "missing.md",
        default_prompt="(no prompt)",
    )
    request = turn.build(agent_ctx)
    contents = [m.content for m in request.system_messages]
    assert contents == ["(no prompt)"]


def test_turn_builder_system_prompt_dir(
    agent_ctx: AgentContext,
    tmp_path,
):
    """.system_prompt_from_directory(dir_path) читает файлы как отдельные блоки."""
    (tmp_path / "01-persona.md").write_text("Ты — Claude.", encoding="utf-8")
    (tmp_path / "02-rules.md").write_text("Отвечай по-русски.", encoding="utf-8")
    (tmp_path / "empty.md").write_text("", encoding="utf-8")

    turn = TurnBuilder("test-model").system_prompt_from_dir(tmp_path)
    request = turn.build(agent_ctx)
    contents = [m.content for m in request.system_messages]
    assert contents == ["Ты — Claude.", "Отвечай по-русски."]


def test_turn_builder_system_prompt_dir_extension_filter(
    agent_ctx: AgentContext,
    tmp_path,
):
    """extensions оставляет только файлы с указанными расширениями."""
    (tmp_path / "keep.md").write_text("kept", encoding="utf-8")
    (tmp_path / "ignore.log").write_text("logged", encoding="utf-8")

    turn = TurnBuilder("test-model").system_prompt_from_dir(tmp_path)
    request = turn.build(agent_ctx)
    contents = [m.content for m in request.system_messages]
    assert contents == ["kept"]


def test_turn_builder_system_prompt_dir_empty_workspace(
    agent_ctx: AgentContext,
    tmp_path,
):
    """Пустая директория — ноль блоков, без ошибки."""
    turn = TurnBuilder("test-model").system_prompt_from_dir(tmp_path)
    request = turn.build(agent_ctx)
    assert request.system_messages == ()


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

    turn = (
        TurnBuilder("test-model")
        .system_prompt("static-block")
        .system_prompt_from_file(ws_persona_root / "persona.md")
        .system_prompt_from_dir(ws_rules_root)
    )
    request = turn.build(agent_ctx)
    contents = [m.content for m in request.system_messages]
    assert contents == ["static-block", "persona-from-file", "rule-from-file"]


def test_turn_builder_extra_overrides_built_in_by_id(agent_ctx: AgentContext):
    """use_reducer с id встроенного reducer'а заменяет его эффект в запросе."""

    class _OverrideModel(PrioritySource[str, TurnState]):
        ID: ClassVar[str] = ModelFromRequestReducer.ID

        def id(self) -> str:
            return self.ID

        def priority(self) -> int:
            return 1

        def apply(self, state: TurnState) -> TurnState:
            state.model = "overridden"
            return state

    override = _OverrideModel()
    turn = TurnBuilder("test-model").use_reducer(override)
    request = turn.build(agent_ctx)
    assert request.model == "overridden"
