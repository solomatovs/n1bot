"""Параметризация TurnSpec через TurnBuilder + AgentBuilder.use_turn()."""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.agent.agent import AgentContext
from boba.agent.builder import AgentBuilder
from boba.agent.messages import InMemoryMessageService
from boba.agent.middleware.llm import LLMPort
from boba.agent.turn.builder import TurnBuilder, TurnSpecBuilder
from boba.agent.turn.reducers import (
    HistoryReducer,
    ModelReducer,
    RememberUserQueryReducer,
    SystemPromptReducer,
)
from boba.agent.turn.spec import TurnState
from boba.patterns import PrioritySource
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolRegistry


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


# TurnSpecBuilder (low-level): LLMPort ничего не знает про конкретные reducer'ы.


def test_middleware_delegates_spec_construction_to_builder(
    agent_ctx: AgentContext,
):
    captured: list[AgentContext] = []

    def factory(ctx: AgentContext) -> _MarkerReducer:
        captured.append(ctx)
        return _MarkerReducer()

    spec_builder = TurnSpecBuilder()
    spec_builder.add(factory)

    mw = LLMPort(
        llm=None,  # type: ignore[arg-type]
        turn_spec_builder=spec_builder,
    )
    spec = mw._turn_spec_builder.build(agent_ctx)

    ids = {p.id() for p in spec.providers()}
    assert _MarkerReducer.ID in ids
    assert captured == [agent_ctx]


# TurnBuilder (high-level)


def test_turn_builder_default_reducers_set(agent_ctx: AgentContext):
    registry = _empty_catalog()
    turn = (
        TurnBuilder()
        .with_model("test-model")
        .with_messages(InMemoryMessageService())
        .with_tool_catalog(registry.catalog())
        .use_default_reducers()
    )
    spec = turn.build_spec_builder().build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    expected = {
        ModelReducer.ID,
        SystemPromptReducer.ID,
        HistoryReducer.ID,
        "tools",
        "sampling",
    }
    assert ids == expected


def test_turn_builder_use_reducer_accepts_ready(agent_ctx: AgentContext):
    marker = _MarkerReducer()
    turn = TurnBuilder().use_reducer(marker)
    spec = turn.build_spec_builder().build(agent_ctx)
    providers = {p.id(): p for p in spec.providers()}
    assert providers[_MarkerReducer.ID] is marker


def test_turn_builder_use_reducer_accepts_factory(agent_ctx: AgentContext):
    turn = TurnBuilder().use_reducer(lambda _ctx: _MarkerReducer())
    spec = turn.build_spec_builder().build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    assert _MarkerReducer.ID in ids


def test_turn_builder_default_plus_extra(agent_ctx: AgentContext):
    registry = _empty_catalog()
    turn = (
        TurnBuilder()
        .with_model("test-model")
        .with_messages(InMemoryMessageService())
        .with_tool_catalog(registry.catalog())
        .use_default_reducers()
        .use_reducer(RememberUserQueryReducer())
    )
    spec = turn.build_spec_builder().build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    assert ModelReducer.ID in ids
    assert RememberUserQueryReducer.ID in ids


def test_turn_builder_user_only_skips_default(agent_ctx: AgentContext):
    turn = TurnBuilder().use_reducer(_MarkerReducer())
    spec = turn.build_spec_builder().build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    # Без use_default_reducers() — только то, что задал пользователь.
    assert ids == {_MarkerReducer.ID}


def test_turn_builder_extra_overrides_default_by_id(agent_ctx: AgentContext):
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
    registry = _empty_catalog()
    turn = (
        TurnBuilder()
        .with_model("test-model")
        .with_messages(InMemoryMessageService())
        .with_tool_catalog(registry.catalog())
        .use_default_reducers()
        .use_reducer(override)
    )
    spec = turn.build_spec_builder().build(agent_ctx)
    providers = {p.id(): p for p in spec.providers()}
    assert providers[ModelReducer.ID] is override


def test_turn_builder_empty_raises():
    with pytest.raises(ValueError, match="ни одного reducer"):
        TurnBuilder().build_spec_builder()


def test_turn_builder_defaults_require_model():
    registry = _empty_catalog()
    turn = (
        TurnBuilder()
        .with_messages(InMemoryMessageService())
        .with_tool_catalog(registry.catalog())
        .use_default_reducers()
    )
    with pytest.raises(ValueError, match="with_model"):
        turn.build_spec_builder()


def test_turn_builder_defaults_require_messages():
    registry = _empty_catalog()
    turn = (
        TurnBuilder()
        .with_model("test-model")
        .with_tool_catalog(registry.catalog())
        .use_default_reducers()
    )
    with pytest.raises(ValueError, match="with_messages"):
        turn.build_spec_builder()


def test_turn_builder_defaults_require_tool_catalog():
    turn = (
        TurnBuilder()
        .with_model("test-model")
        .with_messages(InMemoryMessageService())
        .use_default_reducers()
    )
    with pytest.raises(ValueError, match="with_tool_catalog"):
        turn.build_spec_builder()


# AgentBuilder.use_turn() auto-wiring


def test_agent_builder_use_turn_autowires_messages_and_catalog():
    """Если TurnBuilder не задал messages/catalog — AgentBuilder подкладывает свои."""
    messages = InMemoryMessageService()
    turn = TurnBuilder().with_model("test-model").use_default_reducers()
    registry = _empty_catalog()
    builder = AgentBuilder().with_messages(messages).with_tools(registry).use_turn(turn)
    # Имитируем wiring, который происходит внутри build():
    resolved = builder.tool_registry()
    if not turn.has_messages():
        turn.with_messages(messages)
    if not turn.has_tool_catalog():
        turn.with_tool_catalog(resolved.catalog())
    assert turn.has_messages()
    assert turn.has_tool_catalog()


def test_agent_builder_use_turn_respects_explicit_resources():
    """Явно заданное в TurnBuilder не перетирается AgentBuilder'ом."""
    explicit_messages = InMemoryMessageService()
    turn = (
        TurnBuilder()
        .with_model("test-model")
        .with_messages(explicit_messages)
        .use_default_reducers()
    )
    other_messages = InMemoryMessageService()
    builder = AgentBuilder().with_messages(other_messages).use_turn(turn)
    if not turn.has_messages():
        turn.with_messages(builder._message_service)
    # has_messages был True, перетирания не было.
    assert turn._messages is explicit_messages
