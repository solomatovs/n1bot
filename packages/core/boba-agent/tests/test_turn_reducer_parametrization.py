"""Параметризация TurnSpec через AgentBuilder.use_turn_reducer + middleware."""

from __future__ import annotations

from typing import ClassVar

from boba.agent.agent import AgentContext
from boba.agent.builder import AgentBuilder
from boba.agent.middleware.llm import LLMPort
from boba.agent.turn.builder import TurnSpecBuilder
from boba.agent.turn.reducers import (
    HistoryReducer,
    ModelReducer,
    RememberUserQueryReducer,
    SystemPromptReducer,
)
from boba.agent.turn.spec import TurnState
from boba.patterns import PrioritySource


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


# LLMPort: терминал ничего не знает про конкретные reducer'ы


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


# AgentBuilder.use_turn_reducer / use_default_turn_reducers


def test_builder_default_reducers_registered_when_user_silent(
    agent_ctx: AgentContext,
):
    builder = AgentBuilder().with_model("test-model")
    builder.use_default_turn_reducers()
    spec = builder._turn_spec_builder.build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    expected = {
        ModelReducer.ID,
        SystemPromptReducer.ID,
        HistoryReducer.ID,
        "tools",
        "sampling",
    }
    assert ids == expected


def test_builder_use_turn_reducer_accepts_ready_reducer(
    agent_ctx: AgentContext,
):
    builder = AgentBuilder()
    marker = _MarkerReducer()
    builder.use_turn_reducer(marker)
    spec = builder._turn_spec_builder.build(agent_ctx)
    providers = {p.id(): p for p in spec.providers()}
    assert providers[_MarkerReducer.ID] is marker


def test_builder_use_turn_reducer_accepts_factory(
    agent_ctx: AgentContext,
):
    builder = AgentBuilder()
    builder.use_turn_reducer(lambda _ctx: _MarkerReducer())
    spec = builder._turn_spec_builder.build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    assert _MarkerReducer.ID in ids


def test_builder_default_plus_extra(
    agent_ctx: AgentContext,
):
    builder = AgentBuilder().with_model("test-model")
    (
        builder
        .use_default_turn_reducers()
        .use_turn_reducer(RememberUserQueryReducer())
    )
    spec = builder._turn_spec_builder.build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    assert ModelReducer.ID in ids
    assert RememberUserQueryReducer.ID in ids


def test_builder_user_only_skips_default(
    agent_ctx: AgentContext,
):
    builder = AgentBuilder()
    builder.use_turn_reducer(_MarkerReducer())
    spec = builder._turn_spec_builder.build(agent_ctx)
    ids = {p.id() for p in spec.providers()}
    # Стандартного набора не должно быть — пользователь явно задал только маркер.
    assert ids == {_MarkerReducer.ID}


def test_builder_extra_reducer_with_same_id_overrides_default(
    agent_ctx: AgentContext,
):
    builder = AgentBuilder().with_model("test-model")

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
    builder.use_default_turn_reducers().use_turn_reducer(override)
    spec = builder._turn_spec_builder.build(agent_ctx)
    providers = {p.id(): p for p in spec.providers()}
    assert providers[ModelReducer.ID] is override
