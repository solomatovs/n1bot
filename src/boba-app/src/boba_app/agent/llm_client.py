"""Парсинг streaming-ответа LLM.

Три StreamTransformer[CompletionDelta, DocPipelineEvent]:
    ToolCallAccumulator — накопление tool_calls
    ReasoningExtractor  — извлечение reasoning_content
    ContentParser       — парсинг content (<think> теги)

LLMStreamConsumer прогоняет каждый delta через все три (fan-out).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Sequence

from boba_domain.agent.events import AnswerToken, DocPipelineEvent, ThinkingToken
from boba_domain.agent.think_parser import ThinkTagParser
from boba_domain.core.llm_service import CompletionDelta
from boba_domain.core.streaming import StreamTransformer


# ---------------------------------------------------------------------------
# ToolCallData
# ---------------------------------------------------------------------------

@dataclass
class ToolCallData:
    """Один tool call от LLM."""
    id: str = ""
    name: str = ""
    arguments: str = ""

    def parse_arguments(self) -> Dict[str, Any]:
        if not self.arguments:
            return {}
        try:
            return json.loads(self.arguments)
        except json.JSONDecodeError:
            return {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


# ---------------------------------------------------------------------------
# StreamTransformer[CompletionDelta, DocPipelineEvent] — три реализации
# ---------------------------------------------------------------------------

class ToolCallAccumulator(StreamTransformer[CompletionDelta, DocPipelineEvent]):
    """Накапливает tool_calls из delta. Не yield'ит events.

    Результат — в .tool_calls после завершения потока.
    """

    def __init__(self) -> None:
        self._pending: Dict[int, ToolCallData] = {}

    def feed(self, delta: CompletionDelta) -> Iterator[DocPipelineEvent]:
        if delta.tool_call_index is None:
            return
        idx = delta.tool_call_index
        if idx not in self._pending:
            self._pending[idx] = ToolCallData()
        tc = self._pending[idx]
        if delta.tool_call_id:
            tc.id = delta.tool_call_id
        if delta.tool_call_name:
            tc.name = delta.tool_call_name
        if delta.tool_call_arguments:
            tc.arguments += delta.tool_call_arguments
        yield from ()

    @property
    def tool_calls(self) -> List[ToolCallData]:
        return [self._pending[i] for i in sorted(self._pending)]

    @property
    def has_tool_calls(self) -> bool:
        return len(self._pending) > 0

    def reset(self) -> None:
        self._pending.clear()


class ReasoningExtractor(StreamTransformer[CompletionDelta, DocPipelineEvent]):
    """Извлекает reasoning_content из delta → ThinkingToken.

    Для моделей с отдельным полем reasoning (DeepSeek, QwQ).
    """

    def feed(self, delta: CompletionDelta) -> Iterator[DocPipelineEvent]:
        if delta.reasoning_content:
            yield ThinkingToken(token=delta.reasoning_content)

    def reset(self) -> None:
        pass


class ContentParser(StreamTransformer[CompletionDelta, DocPipelineEvent]):
    """Парсит content из delta через ThinkTagParser → ThinkingToken / AnswerToken."""

    def __init__(self) -> None:
        self._parser = ThinkTagParser()
        self._answer_tokens: List[str] = []

    def feed(self, delta: CompletionDelta) -> Iterator[DocPipelineEvent]:
        content = delta.content or ""
        if not content:
            return
        for fragment in self._parser.feed(content):
            if not fragment.text:
                continue
            if fragment.role.value == "thinking":
                yield ThinkingToken(token=fragment.text)
            else:
                self._answer_tokens.append(fragment.text)
                yield AnswerToken(token=fragment.text)

    @property
    def answer(self) -> str:
        return "".join(self._answer_tokens)

    def reset(self) -> None:
        self._parser.reset()
        self._answer_tokens.clear()


# Тип-алиас для всех delta handlers
DeltaHandler = StreamTransformer[CompletionDelta, DocPipelineEvent]


# ---------------------------------------------------------------------------
# LLMStreamConsumer — fan-out через StreamTransformers
# ---------------------------------------------------------------------------

@dataclass
class LLMStreamConsumer:
    """StreamConsumer: прогоняет каждый delta через набор handlers."""
    tool_calls: List[ToolCallData] = field(default_factory=list)
    answer: str = ""

    def consume(self, deltas: Iterator[CompletionDelta]) -> Iterator[DocPipelineEvent]:
        tc = ToolCallAccumulator()
        reasoning = ReasoningExtractor()
        content = ContentParser()
        handlers: Sequence[DeltaHandler] = [tc, reasoning, content]

        for delta in deltas:
            for handler in handlers:
                yield from handler.feed(delta)

        self.tool_calls = tc.tool_calls
        self.answer = content.answer

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0
