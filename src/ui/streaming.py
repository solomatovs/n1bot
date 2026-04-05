"""Stream parser for LLM responses — Strategy pattern for thinking extraction."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import streamlit as st
from streamlit.delta_generator import DeltaGenerator


# ---------------------------------------------------------------------------
# Strategy: how to extract "thinking" from a stream chunk
# ---------------------------------------------------------------------------

class ThinkingStrategy(ABC):
    """Base class for thinking-extraction strategies."""

    @abstractmethod
    def process(self, state: StreamState, delta: object) -> None:
        """Mutate *state* with data extracted from *delta*."""


class ReasoningFieldStrategy(ThinkingStrategy):
    """Extract thinking from ``delta.reasoning_content`` (DeepSeek / o1 via LiteLLM)."""

    def process(self, state: StreamState, delta: object) -> None:
        reasoning = getattr(delta, "reasoning_content", None) or ""
        if reasoning:
            state.thinking += reasoning
            state.reasoning_active = True
            state.thinking_dirty = True
        elif state.reasoning_active:
            # reasoning_content перестало приходить — модель закончила думать
            content = getattr(delta, "content", None) or ""
            if content:
                state.reasoning_active = False


class ThinkTagStrategy(ThinkingStrategy):
    """Extract thinking from inline ``<think>...</think>`` tags inside ``delta.content``."""

    def process(self, state: StreamState, delta: object) -> None:
        token = getattr(delta, "content", None) or ""
        if not token:
            return

        buf = token
        while buf:
            if state.in_think_tag:
                end = buf.find("</think>")
                if end != -1:
                    state.thinking += buf[:end]
                    buf = buf[end + len("</think>"):]
                    state.in_think_tag = False
                    state.thinking_dirty = True
                else:
                    state.thinking += buf
                    buf = ""
                    state.thinking_dirty = True
            else:
                start = buf.find("<think>")
                if start != -1:
                    state.answer += buf[:start]
                    buf = buf[start + len("<think>"):]
                    state.in_think_tag = True
                    state.answer_dirty = True
                else:
                    state.answer += buf
                    buf = ""
                    state.answer_dirty = True


# ---------------------------------------------------------------------------
# Mutable state accumulated during streaming
# ---------------------------------------------------------------------------

@dataclass
class StreamState:
    thinking: str = ""
    answer: str = ""
    in_think_tag: bool = False
    reasoning_active: bool = False
    thinking_dirty: bool = False
    answer_dirty: bool = False

    @property
    def thinking_in_progress(self) -> bool:
        return self.in_think_tag or self.reasoning_active

    def reset_dirty(self) -> None:
        self.thinking_dirty = False
        self.answer_dirty = False


# ---------------------------------------------------------------------------
# StreamRenderer — orchestrates strategies and Streamlit placeholders
# ---------------------------------------------------------------------------

class StreamRenderer:
    """Orchestrates streaming: runs strategies, flushes to Streamlit placeholders."""

    STRATEGIES: list[type[ThinkingStrategy]] = [ReasoningFieldStrategy, ThinkTagStrategy]

    def __init__(self, container: DeltaGenerator) -> None:
        self._strategies = [cls() for cls in self.STRATEGIES]
        self._state = StreamState()

        self._thinking_expander = container.expander("Процесс размышления", expanded=True)
        self._thinking_ph = self._thinking_expander.empty()
        self._answer_ph = container.empty()

    @property
    def state(self) -> StreamState:
        return self._state

    def feed(self, delta: object) -> None:
        """Process one streaming delta through all strategies, then flush UI."""
        self._state.reset_dirty()
        for strategy in self._strategies:
            strategy.process(self._state, delta)
        self._flush()

    def finalise(self) -> None:
        """Call once after the stream ends to clean up placeholders."""
        if self._state.thinking.strip():
            self._thinking_ph.markdown(self._state.thinking)
        else:
            self._thinking_expander.empty()

        if self._state.answer.strip():
            self._answer_ph.markdown(self._state.answer)

    def set_answer(self, text: str) -> None:
        """Override the answer text (e.g. to append sources)."""
        self._state.answer = text
        self._answer_ph.markdown(text)

    # -- private ---------------------------------------------------------------

    def _flush(self) -> None:
        s = self._state
        if s.thinking_dirty and s.thinking:
            cursor = "▌" if s.thinking_in_progress else ""
            self._thinking_ph.markdown(s.thinking + cursor)
        if s.answer_dirty and s.answer.strip():
            self._answer_ph.markdown(s.answer + "▌")
