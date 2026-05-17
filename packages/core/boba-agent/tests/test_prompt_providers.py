"""Тесты для CallablePromptProvider и WrappingPromptProvider."""

from __future__ import annotations

from boba.agent.prompt import PromptId, PromptState
from boba.agent.prompt_providers import (
    CallablePromptProvider,
    StaticPromptProvider,
    WrappingPromptProvider,
)


def test_callable_provider_invokes_fn_each_call():
    counter = {"n": 0}

    def fn() -> str:
        counter["n"] += 1
        return f"call-{counter['n']}"

    provider = CallablePromptProvider(PromptId("dyn"), priority=10, fn=fn)

    state = PromptState()
    provider.apply(state)
    provider.apply(state)

    assert [b.content for b in state.blocks] == ["call-1", "call-2"]


def test_wrapping_provider_wraps_inner_blocks():
    inner = StaticPromptProvider(PromptId("role"), 10, "senior dev")
    wrapping = WrappingPromptProvider(
        PromptId("role-wrapped"),
        priority=20,
        inner=inner,
        prefix="<your_role>\n",
        suffix="\n</your_role>",
    )

    state = PromptState()
    wrapping.apply(state)

    assert len(state.blocks) == 1
    assert state.blocks[0].content == "<your_role>\nsenior dev\n</your_role>"


def test_wrapping_provider_has_own_id_and_priority():
    inner = StaticPromptProvider(PromptId("inner"), 10, "x")
    wrapping = WrappingPromptProvider(
        PromptId("outer"),
        priority=99,
        inner=inner,
    )

    assert wrapping.id() == "outer"
    assert wrapping.priority() == 99


def test_wrapping_provider_empty_prefix_suffix_is_passthrough():
    inner = StaticPromptProvider(PromptId("inner"), 10, "hello")
    wrapping = WrappingPromptProvider(
        PromptId("outer"),
        priority=20,
        inner=inner,
    )

    state = PromptState()
    wrapping.apply(state)

    assert state.blocks[0].content == "hello"
